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


@dataclass
class PlaybackEvent:
    """状态机输入事件。epoch 为代际号：异步结果必须携带发起时的代际号。"""

    type: PlaybackEventType
    track: Optional[dict[str, Any]] = None
    epoch: int = 0
    error_msg: Optional[str] = None


@dataclass
class PlaybackEffect:
    """待执行的副作用声明（reducer 只声明、绝不执行）。"""

    type: PlaybackEffectType
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaybackState:
    """播放状态快照（不可变：reducer 每次返回新实例）。"""

    status: PlaybackStatus = PlaybackStatus.IDLE
    current_track: Optional[dict[str, Any]] = None
    epoch: int = 0
    error_msg: Optional[str] = None


def _same_epoch(state: PlaybackState, event: PlaybackEvent) -> bool:
    """代际号匹配：异步结果只有在仍代表"当前播放意图"时才生效。"""
    return event.epoch == state.epoch


def reduce(
    state: PlaybackState, event: PlaybackEvent
) -> tuple[PlaybackState, list[PlaybackEffect]]:
    """纯 reducer：(state, event) -> (new_state, effects)。

    严禁在函数内发起 IO / 网络 / mpv 调用；一切副作用只以 effects 返回。
    """
    effects: list[PlaybackEffect] = []

    # ---- 意图事件 ----
    if event.type == PlaybackEventType.PLAY_REQUESTED:
        # 新播放意图：进入加载态，epoch 递增（作废一切在途的旧异步结果）
        return PlaybackState(
            status=PlaybackStatus.LOADING,
            current_track=event.track,
            epoch=state.epoch + 1,
            error_msg=None,
        ), [PlaybackEffect(PlaybackEffectType.LOAD_FILE, {"track": event.track})]

    if event.type == PlaybackEventType.PAUSE_REQUESTED:
        # 仅播放中可暂停
        if state.status != PlaybackStatus.PLAYING:
            return state, effects
        return PlaybackState(
            status=PlaybackStatus.PAUSED,
            current_track=state.current_track,
            epoch=state.epoch,
            error_msg=None,
        ), [PlaybackEffect(PlaybackEffectType.PAUSE_MPV)]

    if event.type == PlaybackEventType.RESUME_REQUESTED:
        # 仅暂停态可恢复
        if state.status != PlaybackStatus.PAUSED:
            return state, effects
        return PlaybackState(
            status=PlaybackStatus.PLAYING,
            current_track=state.current_track,
            epoch=state.epoch,
            error_msg=None,
        ), [PlaybackEffect(PlaybackEffectType.RESUME_MPV)]

    if event.type == PlaybackEventType.STOP_REQUESTED:
        return PlaybackState(
            status=PlaybackStatus.IDLE,
            current_track=None,
            epoch=state.epoch,
            error_msg=None,
        ), [PlaybackEffect(PlaybackEffectType.STOP_MPV)]

    # ---- 异步结果事件（epoch 契约） ----
    if event.type == PlaybackEventType.LOAD_SUCCESS:
        if not _same_epoch(state, event):
            # 过期结果：已被更新的播放请求覆盖，丢弃（防旧覆盖新）
            return state, effects
        return PlaybackState(
            status=PlaybackStatus.PLAYING,
            current_track=event.track or state.current_track,
            epoch=state.epoch,
            error_msg=None,
        ), effects

    if event.type == PlaybackEventType.LOAD_FAILED:
        if not _same_epoch(state, event):
            # 过期失败（可能已被新请求覆盖），丢弃
            return state, effects
        return PlaybackState(
            status=PlaybackStatus.FAILED,
            current_track=state.current_track,
            epoch=state.epoch,
            error_msg=event.error_msg,
        ), [PlaybackEffect(
            PlaybackEffectType.NOTIFY_UI, {"error_msg": event.error_msg}
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
        return PlaybackState(
            status=PlaybackStatus.IDLE,
            current_track=None,
            epoch=state.epoch,
            error_msg=None,
        ), [PlaybackEffect(
            PlaybackEffectType.SCHEDULE_NEXT_EPISODE,
            {"track": state.current_track},
        )]

    # 未知事件：保持原状态（防御性，不产生副作用）
    return state, effects
