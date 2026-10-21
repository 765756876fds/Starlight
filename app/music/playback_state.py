"""纯函数播放状态机（reducer 模式）——Controller 拆分第三步。

抽离 MusicController 的播放状态流转为纯逻辑层：
    (state, event) -> (new_state, effects)

铁律：
- reducer 内严禁任何 IO / 网络 / mpv 调用；一切"要做什么"只以 PlaybackEffect 列表返回。
- epoch（代际号）契约：异步结果事件（LOAD_SUCCESS / LOAD_FAILED / IDLE_DETECTED /
  EOF_DETECTED）必须与 state.epoch 匹配才生效，否则视为过期事件直接丢弃——
  防止旧网络解析/旧播放结果覆盖用户最新播放意图。
- 本模块尚未接入现有执行链路，由后续 Controller 适配层消费。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class PlaybackStatus(str, Enum):
    """播放状态：空闲 / 加载中 / 播放中 / 已暂停 / 加载失败。"""

    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    FAILED = "failed"


class PlaybackEventType(str, Enum):
    """状态机输入事件（意图 + 异步结果）。"""

    PLAY_REQUESTED = "play_requested"
    LOAD_SUCCESS = "load_success"
    LOAD_FAILED = "load_failed"
    IDLE_DETECTED = "idle_detected"
    EOF_DETECTED = "eof_detected"
    PAUSE_REQUESTED = "pause_requested"
    RESUME_REQUESTED = "resume_requested"
    STOP_REQUESTED = "stop_requested"


class PlaybackEffectType(str, Enum):
    """副作用类型：reducer 不执行，只声明；由外部适配层（Controller）执行。"""

    LOAD_FILE = "load_file"
    PAUSE_MPV = "pause_mpv"
    RESUME_MPV = "resume_mpv"
    STOP_MPV = "stop_mpv"
    SCHEDULE_NEXT_EPISODE = "schedule_next_episode"
    NOTIFY_UI = "notify_ui"
    ADVANCE_QUEUE = "advance_queue"


@dataclass
class PlaybackEvent:
    """状态机输入事件。epoch 为代际号：异步结果必须携带发起时的代际号。
    origin（仅 PLAY_REQUESTED 使用）："user"（用户主动切歌）或 "auto"（自动切歌）。"""

    type: PlaybackEventType
    track: Optional[dict[str, Any]] = None
    epoch: int = 0
    error_msg: Optional[str] = None
    origin: str = "user"


@dataclass
class PlaybackEffect:
    """待执行的副作用声明（reducer 只声明、绝不执行）。"""

    type: PlaybackEffectType
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaybackState:
    """播放状态快照（不可变：reducer 每次返回新实例）。
    fail_streak：连续加载失败计数（>=3 触发熔断，停止自动重试）。"""

    status: PlaybackStatus = PlaybackStatus.IDLE
    current_track: Optional[dict[str, Any]] = None
    epoch: int = 0
    error_msg: Optional[str] = None
    fail_streak: int = 0


def _same_epoch(state: PlaybackState, event: PlaybackEvent) -> bool:
    """代际号匹配：异步结果只有在仍代表"当前播放意图"时才生效。"""
    return event.epoch == state.epoch


def reduce(
    state: PlaybackState, event: PlaybackEvent
) -> tuple[PlaybackState, list[PlaybackEffect]]:
    """纯 reducer：(state, event) -> (new_state, effects)。

    严禁在函数内发起 IO / 网络 / mpv 调用；一切副作用只以 effects 返回。
    统一用 dataclasses.replace 构造新状态，避免遗漏字段。
    """
    from dataclasses import replace

    effects: list[PlaybackEffect] = []

    # ---- 意图事件 ----
    if event.type == PlaybackEventType.PLAY_REQUESTED:
        # 新播放意图：进入加载态，epoch 递增（作废一切在途的旧异步结果）。
        # 熔断语义：用户主动切歌清零失败计数；自动切歌保留（连续失败由 auto 累积）。
        new_fail_streak = 0 if event.origin == "user" else state.fail_streak
        return replace(
            state,
            status=PlaybackStatus.LOADING,
            current_track=event.track,
            epoch=state.epoch + 1,
            error_msg=None,
            fail_streak=new_fail_streak,
        ), [PlaybackEffect(PlaybackEffectType.LOAD_FILE, {"track": event.track})]

    if event.type == PlaybackEventType.PAUSE_REQUESTED:
        # 仅播放中可暂停
        if state.status != PlaybackStatus.PLAYING:
            return state, effects
        return replace(
            state,
            status=PlaybackStatus.PAUSED,
            error_msg=None,
        ), [PlaybackEffect(PlaybackEffectType.PAUSE_MPV)]

    if event.type == PlaybackEventType.RESUME_REQUESTED:
        # 仅暂停态可恢复
        if state.status != PlaybackStatus.PAUSED:
            return state, effects
        return replace(
            state,
            status=PlaybackStatus.PLAYING,
            error_msg=None,
        ), [PlaybackEffect(PlaybackEffectType.RESUME_MPV)]

    if event.type == PlaybackEventType.STOP_REQUESTED:
        return replace(
            state,
            status=PlaybackStatus.IDLE,
            current_track=None,
            error_msg=None,
        ), [PlaybackEffect(PlaybackEffectType.STOP_MPV)]

    # ---- 异步结果事件（epoch 契约 + 状态防污染） ----
    if event.type == PlaybackEventType.LOAD_SUCCESS:
        # 拦截条件：epoch 匹配 且 当前确在 LOADING。
        # 双条件防污染：PAUSED/PLAYING 收到迟到的 LOAD_SUCCESS 绝不能变回/维持错误状态。
        if not _same_epoch(state, event) or state.status != PlaybackStatus.LOADING:
            return state, effects
        return replace(
            state,
            status=PlaybackStatus.PLAYING,
            current_track=event.track or state.current_track,
            error_msg=None,
            fail_streak=0,  # 加载成功清零连续失败计数
        ), effects

    if event.type == PlaybackEventType.LOAD_FAILED:
        # 同 LOAD_SUCCESS：epoch 匹配 且 LOADING 才生效（防迟到失败污染）
        if not _same_epoch(state, event) or state.status != PlaybackStatus.LOADING:
            return state, effects
        new_streak = state.fail_streak + 1
        if new_streak >= 3:
            # 熔断：连续 3 次失败，停止自动重试——转 IDLE + 通知 UI，不产生 ADVANCE_QUEUE
            return replace(
                state,
                status=PlaybackStatus.IDLE,
                current_track=None,
                error_msg=event.error_msg,
                fail_streak=new_streak,
            ), [PlaybackEffect(
                PlaybackEffectType.NOTIFY_UI,
                {"error_msg": f"连续 {new_streak} 次加载失败，已停止播放"},
            )]
        # 未达熔断阈值：转 FAILED 并请求队列前进（自动跳过坏歌）
        return replace(
            state,
            status=PlaybackStatus.FAILED,
            error_msg=event.error_msg,
            fail_streak=new_streak,
        ), [PlaybackEffect(
            PlaybackEffectType.ADVANCE_QUEUE, {"reason": "load_failed"}
        )]

    # 播放结束：仅在 PLAYING 且 epoch 匹配时生效（防旧检测误切下一首）
    if event.type in (
        PlaybackEventType.IDLE_DETECTED,
        PlaybackEventType.EOF_DETECTED,
    ):
        if state.status != PlaybackStatus.PLAYING:
            return state, effects
        if not _same_epoch(state, event):
            return state, effects
        return replace(
            state,
            status=PlaybackStatus.IDLE,
            current_track=None,
            error_msg=None,
        ), [PlaybackEffect(
            PlaybackEffectType.SCHEDULE_NEXT_EPISODE,
            {"track": state.current_track},
        )]

    # 未知事件：保持原状态（防御性，不产生副作用）
    return state, effects


# 加载确认宽限期：本地文件解码快（0.8s），在线流媒体握手慢（3.0s）
_LOAD_GRACE_SEC = {"local": 0.8, "online": 3.0}
_ONLINE_TIMEOUT_SEC = 10.0


def classify_status(
    state: PlaybackState,
    sample: dict[str, Any],
    elapsed_sec: float,
    kind: str,
) -> Optional[PlaybackEvent]:
    """纯函数分类器：根据 mpv 采样把"原始遥测"翻译成状态机事件。

    sample 包含：idle (bool)、playback_time (float)、duration (float)。
    elapsed_sec：距离 loadfile 发起过去了多少秒。
    kind：文件类型（"local" 或 "online"）。

    规则：
    - LOADING：宽限期（local 0.8s / online 3.0s）内返回 None 等待；
      过宽限期后 idle=True → LOAD_FAILED；playback_time/duration > 0 → LOAD_SUCCESS；
      online 超 10s 仍未起播 → LOAD_FAILED（超时）。
    - PLAYING：idle=True → IDLE_DETECTED（播放自然结束）。
    - 其他状态：None。

    返回的事件携带当前 epoch（异步结果必须与 state.epoch 匹配才被 reducer 接受）。
    本函数纯计算，不做任何 IO。
    """
    idle = bool(sample.get("idle", False))
    playback_time = float(sample.get("playback_time") or 0.0)
    duration = float(sample.get("duration") or 0.0)

    if state.status == PlaybackStatus.LOADING:
        grace = _LOAD_GRACE_SEC.get(kind, 3.0)
        if elapsed_sec < grace:
            # 仍在宽限期内：不急于判定，等待 mpv 起播
            return None
        if idle:
            return PlaybackEvent(
                PlaybackEventType.LOAD_FAILED,
                epoch=state.epoch,
                error_msg=f"加载失败（{kind}，宽限期后仍 idle）",
            )
        if playback_time > 0 or duration > 0:
            # 已取得播放时间或时长：确认起播
            return PlaybackEvent(
                PlaybackEventType.LOAD_SUCCESS,
                epoch=state.epoch,
                track=state.current_track,
            )
        if kind == "online" and elapsed_sec > _ONLINE_TIMEOUT_SEC:
            return PlaybackEvent(
                PlaybackEventType.LOAD_FAILED,
                epoch=state.epoch,
                error_msg="在线加载超时",
            )
        return None

    if state.status == PlaybackStatus.PLAYING:
        if idle:
            return PlaybackEvent(
                PlaybackEventType.IDLE_DETECTED,
                epoch=state.epoch,
            )
        return None

    return None
