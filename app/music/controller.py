"""
音乐控制器 - 高层封装，整合 mpv + 队列 + 数据库 + 搜索

对外提供统一的音乐操作接口。
通过 EventBus 发布音乐事件。
通过 CommandBus 接收音乐命令。
"""
import contextlib
import os
import threading
import time
from typing import Any

from ..core.command_bus import Command, CommandType, get_command_bus
from ..core.event_bus import Event, EventType, get_event_bus
from ..core.logger import get_logger
from ..core.state_store import StateNamespace, get_state_store
from .database import MusicDatabase
from .history_recorder import HistoryRecorder
from .lx_client import LxMusicClient
from .mpv import MpvController
from .mpv_adapter import MpvAdapter
from .online.events import ONLINE_ERROR, ONLINE_SEARCH_DONE
from .online.resolver import (
    OnlineResolver,
    _kw_bonus,
    _normalize,
    _pick_best,
    _pick_candidates,
    _pick_play_headers,
    _quality_score,
)
from .playback_state import (
    PlaybackEvent,
    PlaybackEventType,
    PlaybackState,
    classify_status,
    reduce,
)
from .queue import PlayMode, PlayQueue
from .scanner import MusicScanner
from .search import MusicSearch

logger = get_logger()


# 在线解析纯函数（_pick_play_headers/_quality_score/_normalize/_kw_bonus/
# _pick_candidates/_pick_best）已迁往 app/music/online/resolver.py，
# controller 顶部 re-export 以保持调用点与测试 import 兼容。


def _cancel_switch_on_user_action(method):
    """用户操作入口装饰器：先取消进行中的候选换源（用户优先）。"""
    def wrapper(self, *args, **kwargs):
        try:
            self._cancel_candidate_switch()
        except Exception:
            pass
        return method(self, *args, **kwargs)
    return wrapper


class MusicController:
    """
    音乐控制器

    整合所有音乐子模块，提供统一接口。
    """

    def __init__(self, config: dict[str, Any], mpv_adapter: MpvAdapter | None = None):
        self._movie_pauser = None  # 注入：电影暂停确认回调
        """mpv_adapter: 可选注入的播放器替身（满足 MpvAdapter 协议）。
        注入时 controller 不再实例化真实 MpvController——测试可挂
        不发真声音的 Fake，构造/播放全程无 IPC、无子进程。"""
        self.config = config
        # 路径绝对化（P0-2）：相对路径（如 ./data/music.db）基于 PROJECT_ROOT，不依赖 CWD
        from app.core.path_resolve import resolve_under_root
        self.db_path = resolve_under_root(config.get("database", "./data/music.db"))
        self.scan_dirs = config.get("scan_dirs", [])
        self.extensions = set(config.get("extensions", [".mp3", ".flac", ".wav", ".m4a", ".ogg"]))
        # mpv 查找链（打包零配置运行）：settings 显式 → 打包内置 bundled → PATH（下游兜底）
        from app.core.config import resolve_mpv_path
        self.mpv_path = resolve_mpv_path(config.get("mpv_path"))
        self.mpv_ipc_pipe = config.get("mpv_ipc_pipe", "\\\\.\\pipe\\lol_music_mpv")
        self.default_volume = config.get("default_volume", 70)
        # 下载目录：settings.yaml music.download_dir；缺省 = 第一个扫描目录
        self.download_dir = config.get("download_dir") or (self.scan_dirs[0] if self.scan_dirs else ".")

        # 初始化子模块
        self.db = MusicDatabase(self.db_path)
        self.scanner = MusicScanner(self.db, self.scan_dirs, self.extensions, self.mpv_path)
        self.search = MusicSearch(self.db)
        self.queue = PlayQueue()
        # 播放模式记忆：settings.yaml music.play_mode（无 → 保持 queue 默认顺序）
        play_mode = config.get("play_mode")
        if play_mode:
            try:
                self.queue.set_play_mode(str(play_mode))
            except Exception:
                pass  # 非法模式值 → 保持默认，不崩
        self._missing_file_streak = 0   # 连续文件不可用计数（限次保护）
        self._last_next_ts = 0.0        # next() 防抖时间戳（语音+UI 并发双触发合并）
        if mpv_adapter is not None:
            # 外部注入（Fake/替身）：controller 依赖协议而非具体类
            self.mpv = mpv_adapter
        else:
            self.mpv = MpvController(self.mpv_path, self.mpv_ipc_pipe)

        # 影子模式对拍基建：trace_enabled=True 时包一层 RecordingMpv（只记录，不改行为）。
        # 开关读 config/settings.yaml 的 music.trace_enabled，默认关闭。
        # self._trace 保存 recorder 引用（未启用时为 None），供 intent/decision 打点。
        self._trace = None
        if self._load_trace_enabled():
            try:
                from .trace_recorder import get_trace_recorder
                from .recording_mpv import RecordingMpv
                self._trace = get_trace_recorder(enabled=True)
                self.mpv = RecordingMpv(self.mpv, self._trace)
            except Exception:
                self._trace = None
        # origin 上下文（threading.local）：区分用户主动操作（user）与自动流程（auto）
        self._tl = threading.local()

        # 影子状态机（影子模式对拍）：无条件实例化（PlaybackState 极轻）。
        # _dispatch_shadow 以 _trace 守卫保证 trace 关闭时零开销；
        # _shadow_loadfile_ts/_shadow_file_kind 供 classify_status 的
        # elapsed_sec/kind 采样（无 loadfile 时 ts 为 None → elapsed 记 0）。
        self._shadow_state = PlaybackState()
        self._shadow_lock = threading.Lock()
        self._shadow_loadfile_ts = None
        self._shadow_file_kind = "local"

        # 落雪音乐客户端
        self.lx_client = LxMusicClient()
        self.lx_connected = self.lx_client.is_connected()
        if self.lx_connected:
            logger.info('[音乐] 落雪音乐已连接，语音控制将优先控制落雪')
        else:
            logger.info('[音乐] 本地 mpv 播放引擎已就绪')

        # Online sources（延迟构造，可选）
        self.online_manager = None
        # 下载器（online_manager 注入后创建，见 set_online_manager）
        self.downloader = None

        # 核心服务
        self.event_bus = get_event_bus()
        self.command_bus = get_command_bus()
        self.state_store = get_state_store()

        # 状态
        self._running = False
        self._lock = threading.Lock()
        self._current_song: dict[str, Any] | None = None
        # V2-2：播放会话代次，每次加载新媒体递增；全局暂停快照据此判断会话是否变化
        self._playback_gen: int = 0
        # P1 候选旁路：最近一次语音搜歌的候选（事件只带展示，track 本体留本对象）
        self._candidate_generation: int = 0
        self._active_candidates: list = []
        # B 方案：换源取消令牌 + 抑制跳歌标记
        self._candidate_switch_token: int = 0
        self._candidate_switching: bool = False
        # 在线解析器（manager 注入后创建，见 set_online_manager；纯网络解析，不碰 mpv/队列）
        self._resolver: OnlineResolver | None = None
        # 播放请求代际号（request_id 契约）：每次用户发出新的播放意图（本地/在线/切歌）
        # 自增；后台在线解析完成后必须核对代际号，过期即丢弃，防止旧解析覆盖最新播放请求。
        self._play_request_id = 0
        # seek 防抖：seek 指令发出后 mpv 会短暂进入 idle（缓冲/seek 窗口），
        # 若不加拦截，_poll_idle 会把"seek 导致的瞬时 idle"误判为播放结束而自动切歌。
        # 置位/复位均由线程写布尔值（GIL 原子），轮询线程读即可，无需加锁。
        self._is_seeking = False
        # 代次隔离（epoch 契约）：_play_request_id 即最新播放意图代次；
        # _last_loadfile_epoch 记录最后一次真正 loadfile 的代次。
        # 两者不相等 = "切歌已发起但新歌尚未 loadfile"（在线解析窗口）
        # → _poll_idle 必须跳过 playback-time 缓存写入，旧歌实时位置不得污染新歌进度。
        self._last_loadfile_epoch = 0
        # UI 切换态：置 True 期间 UI 禁止渲染旧进度（显示"加载中"），
        # 新歌 loadfile 起播（本地/在线统一入口）后置 False 恢复渲染。
        # 状态位在 __init__ 初始化（start 仅置运行标志）：测试/注入路径不调
        # start() 也能持有完整状态（播放/切歌逻辑直接读取）。
        self._is_switching_track = False
        # 播放时间内存快照（后台轮询/确认线程更新）：GUI 高频刷新（歌词 200ms / 进度条）
        # 只读此快照，绝不再发 mpv IPC——消除 GUI 线程卡顿源。
        # 结构 (pos, perf_counter_ts, paused)：读取时按 (now - ts) 外推，暂停时不外推，
        # 消除 0.8s 采样 × 200ms 读取的阶梯感。
        self._playback_time_cache: tuple = (0.0, 0.0, False)
        self._paused_snapshot: bool = False  # 暂停标志快照（供快照外推判断）
        self._user_stopped: bool = False  # V2-4.5.6：用户主动停止，防止 idle/EOF 自动续播

        # mpv 可用性（mpv 未安装/路径错/启动失败 → 模块降级运行，不崩主程序）
        self._mpv_available = True
        # mpv 自愈状态（崩溃/管道断自动重启）
        self._mpv_fail_streak = 0          # get_property 连续失败计数
        self._mpv_restart_count = 0        # 当前窗口内重启次数
        self._mpv_restart_window_ts = 0.0  # 窗口起点
        self._mpv_gave_up = False          # 超过上限后放弃自动恢复
        self._mpv_restart_pending = False  # 一次重启进行中（防重入 + UI 侧跳过）
        # 播报 ducking（TTS 播报期间压低音乐音量）
        self._duck_active = False
        self._duck_original_volume: int | None = None
        self.duck_volume_pct = int(config.get("duck_volume_pct", 50))

        # Highlight Mode 状态
        self._highlight_mode: bool = False
        self._highlight_gen: int = 0  # 每首歌一个 token，旧 poller 失效
        self._highlight_end_time: float | None = None
        self._highlight_song_id: int | None = None
        self._highlight_stop: threading.Event = threading.Event()
        self._highlight_poller: threading.Thread | None = None
        self._skip_pending: bool = False  # 坏文件自动跳过防重入
        # 播放倍速（mpv speed 属性；切歌保留，mpv 重启后由 mpv 默认 1.0 重置）
        self._speed: float = 1.0
        self._sleep_timer = None
        self._sleep_timer_lock = threading.Lock()
        # UI「音源管理」导入的自定义 HTTP 源，sync_custom_sources() 维护的注册名单
        self._custom_http_sources: set = set()

        # 播放历史记录器（独立模块；start() 时订阅 MUSIC_TRACK_CHANGED）
        self.history_recorder = HistoryRecorder(self.db, self.event_bus)
        # 注册命令处理器
        self._register_commands()

        # 注册 mpv 事件监听
        self.mpv.on_property_change("idle-active", self._on_idle_change)
        self.mpv.on_property_change("eof-reached", self._on_eof)

    def start(self) -> bool:
        """启动音乐模块。mpv 不可用 → 降级运行（不抛异常），返回 False。"""
        self.history_recorder.start()
        if self._running:
            return True

        # 启动 mpv（失败不抛：mpv 未安装/路径错误 → 模块降级，不崩主程序）
        try:
            ok = bool(self.mpv.start())
        except Exception as e:
            logger.info(f"[Music] mpv 启动异常: {e!r}")
            ok = False
        if not ok:
            self._mpv_available = False
            logger.info("[Music] mpv 不可用，音乐模块降级运行（播放/音量控制禁用）")
            self._running = True
            self._subscribe_speech_events()
            self.state_store.set(StateNamespace.MUSIC, "volume", self.default_volume)
            self.state_store.set(StateNamespace.MUSIC, "playing", False)
            self.state_store.set(StateNamespace.MUSIC, "current_song", None)
            # mpv 不可用也要后台算 highlight（不依赖 mpv）
            try:
                self.scanner.start_highlight_background()
            except Exception as e:
                logger.warning(f"[Music] 启动后台 highlight worker 失败: {e!r}")
            return False
        self._mpv_available = True
        self.mpv.set_volume(self.default_volume)

        # 注册事件
        self._running = True
        # 状态位（_is_seeking/_last_loadfile_epoch/_is_switching_track）已在
        # __init__ 初始化（构造即持有，不依赖 start 调用顺序）
        self._subscribe_speech_events()

        # 启动后台轮询：每0.8秒实时查mpv idle状态，播完自动切下一首。
        # 说明：mpv IPC 事件消息在 _send_command 响应循环中被跳过，事件回调
        # （on_property_change → _on_idle_change/_on_eof）实际不可达；自动切歌
        # 必须用独立轮询实现（历史既定方案，实时查询不读属性缓存）。
        # 初始化 idle 基线：mpv 刚冷启动通常处于空闲（idle-active=True）。
        # 必须用启动时的真实值初始化 _last_idle，否则首次轮询会把"启动空闲"
        # 误判为"从播放变为空闲"的上升沿，导致启动即自动播放。
        # 读取失败时保守取 True（宁可少一次自动切歌，也不在启动时自动播放）。
        try:
            self._last_idle = bool(self.mpv.get_property("idle-active", timeout=1.0))
        except Exception:
            self._last_idle = True

        def _poll_idle():
            import time as _time
            while self._running:
                if self._mpv_gave_up:
                    _time.sleep(2.0)
                    continue
                # 1) 管道健康探测：get_property 连续失败 → 触发重启
                try:
                    now_idle = bool(self.mpv.get_property("idle-active", timeout=0.5))
                    self._mpv_fail_streak = 0
                    # 顺带更新播放时间快照（同一轮询线程的 IPC 会话内）
                    # —— 供歌词/进度条读内存，GUI 线程禁调 IPC。
                    if now_idle:
                        # idle 为真：位置归零（避免残留旧值/抖动导致进度跳回开头）
                        self._playback_time_cache = (0.0, _time.perf_counter(), False)
                    else:
                        try:
                            pt = self.mpv.get_property("playback-time", timeout=0.5)
                            self._on_poll_playback_time(pt)
                        except Exception:
                            pass
                    # 影子模式采样（trace 启用时）：把 mpv 遥测喂给影子状态机，
                    # 只记录 effects，绝不执行真实副作用。复用缓存避免重复 IPC。
                    if self._trace and self._trace.enabled:
                        try:
                            sample = {"idle": now_idle}
                            if not now_idle:
                                cached = self._playback_time_cache
                                if cached and cached[0] is not None:
                                    sample["playback_time"] = float(cached[0])
                            shadow_ts = self._shadow_loadfile_ts
                            elapsed = (_time.perf_counter() - shadow_ts) if shadow_ts else 0.0
                            kind = self._shadow_file_kind
                            ev = classify_status(self._shadow_state, sample, elapsed, kind)
                            if ev is not None:
                                self._dispatch_shadow(ev)
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"[Music] idle poll error: {e!r}")
                    self._mpv_fail_streak += 1
                    if self._mpv_fail_streak >= 3:
                        self._mpv_fail_streak = 0
                        self._try_restart_mpv("pipe-fail")
                    _time.sleep(0.8)
                    continue
                # 2) 进程存活探测：mpv 进程死亡 → 触发重启
                try:
                    alive = bool(self.mpv.is_running())
                except Exception:
                    alive = True
                if not alive:
                    logger.info("[Music] mpv 进程已退出")
                    self._try_restart_mpv("process-dead")
                    _time.sleep(0.8)
                    continue
                # 3) 原逻辑：idle 上升沿 → 自动切歌
                if now_idle and self._is_seeking:
                    # seek 防抖：拖拽进度条后 mpv 短暂进入 idle（seek 缓冲窗口），
                    # 属正常现象，绝不能误判为播放结束。不消费上升沿（不更新
                    # _last_idle）：seek 结束恢复播放 → 无事发生；seek 后确实
                    # 播完 → 下轮轮询仍命中上升沿正常切歌。
                    _time.sleep(0.5)
                    continue
                if now_idle and not self._last_idle:
                    _time.sleep(0.5)  # 等 mpv 稳定
                    logger.info("[Music] 检测到播放结束，自动切下一首")
                    self._auto_next()
                self._last_idle = now_idle
                _time.sleep(0.8)

        threading.Thread(target=_poll_idle, daemon=True, name="music-idle-poll").start()

        # 更新状态
        self.state_store.set(StateNamespace.MUSIC, "volume", self.default_volume)
        self.state_store.set(StateNamespace.MUSIC, "playing", False)
        self.state_store.set(StateNamespace.MUSIC, "current_song", None)

        logger.info("[Music] 模块已启动")
        # 启动时不做全量 rescan，只后台挑 pending 歌曲自动算 highlight。
        # manual / 已有 pychorus 记录会在 scanner._highlight_worker 内跳过。
        # 失败不影响主程序：worker 内部已 try/except。
        try:
            self.scanner.start_highlight_background()
        except Exception as e:
            logger.warning(f"[Music] 启动后台 highlight worker 失败: {e!r}")
        return True

    def _subscribe_speech_events(self):
        """订阅 TTS 播报事件（播报期间压低音乐音量）。
        在 start() 里订阅：与模块生命周期一致；测试不 start 则不产生全局订阅残留。"""
        self.event_bus.subscribe(EventType.SPEECH_STARTED, self._on_speech_started)
        self.event_bus.subscribe(EventType.SPEECH_FINISHED, self._on_speech_finished)

    def _on_poll_playback_time(self, pt):
        """轮询拿到 playback-time 后的状态机：None → IPC 静默计数；有效值 → 清零并写缓存。"""
        if pt is None:
            self._mpv_fail_streak += 1
            if self._mpv_fail_streak >= 3:
                self._mpv_fail_streak = 0
                self._try_restart_mpv("ipc-silent")
        elif self._play_request_id == self._last_loadfile_epoch:
            self._mpv_fail_streak = 0
            self._playback_time_cache = (
                float(pt), time.perf_counter(), self._paused_snapshot)

    def _try_restart_mpv(self, reason: str):
        """mpv 崩溃/管道断自愈：后台线程 stop → start(taskkill=False) → 恢复当前歌。
        锁内只做防重入决策（<1ms），实际重启在 daemon 线程——重启耗时最坏 ~6s
        不再持业务锁，UI 播放/切歌操作不被阻塞。
        窗口限制 3 次/5 分钟；超过则放弃（_mpv_gave_up），避免反复崩。"""
        with self._lock:
            if self._mpv_restart_pending:
                return
            now = time.time()
            if now - self._mpv_restart_window_ts > 300:
                self._mpv_restart_window_ts = now
                self._mpv_restart_count = 0
            if self._mpv_restart_count >= 3:
                if not self._mpv_gave_up:
                    self._mpv_gave_up = True
                    logger.error("[Music] mpv 多次重启失败，放弃自动恢复（音乐模块停止自愈）")
                return
            self._mpv_restart_count += 1
            logger.info(f"[Music] mpv 异常({reason})，尝试自动重启 {self._mpv_restart_count}/3")
            self._mpv_restart_pending = True
            # 重启时点快照：歌曲 + 播放请求代次。恢复时必须校验代次——
            # 重启窗口内用户发起新播放（_play_request_id 已变）则旧快照作废。
            restart_song = self._current_song
            restart_req_id = self._play_request_id
        threading.Thread(
            target=self._restart_mpv_worker,
            args=(restart_song, restart_req_id),
            daemon=True,
            name="mpv-restart",
        ).start()

    def _restart_mpv_worker(self, restart_song: dict[str, Any] | None,
                            restart_req_id: int | None = None):
        """后台执行实际重启；状态更新后持锁收尾清 pending。"""
        try:
            try:
                self.mpv.stop_process()
            except Exception:
                pass
            try:
                ok = bool(self.mpv.start(taskkill=False, pipe_timeout=3.0, prop_timeout=0.5))
            except Exception as e:
                logger.info(f"[Music] mpv 重启异常: {e!r}")
                ok = False
            if not ok:
                with self._lock:
                    self._mpv_available = False
                logger.error("[Music] mpv 重启失败，模块降级")
                return
            with self._lock:
                self._mpv_available = True
            # 重置轮询基线：mpv 冷启动处于 idle，必须真实读取（失败保守取 True）
            try:
                self._last_idle = bool(self.mpv.get_property("idle-active", timeout=1.0))
            except Exception:
                self._last_idle = True
            # 重应用用户倍速：新 mpv 进程 speed 重置为默认 1.0，Controller 仍记录
            # 用户上次设定值；在恢复播放前重设，避免重启后无声回到原速（与 UI 显示不一致）。
            try:
                if self._speed and self._speed != 1.0:
                    self.mpv.set_property("speed", float(self._speed))
            except Exception as e:
                logger.info(f"[Music] 重启后重设倍速失败(忽略): {e!r}")
            # 恢复当前歌曲（仅本地文件；在线 URL 交给 idle 上升沿自动切歌）。
            # 用重启时点快照 + 代次校验，避免重启完成时用户已切歌却恢复旧歌。
            self._try_resume_after_restart(restart_song, restart_req_id)
        finally:
            with self._lock:
                self._mpv_restart_pending = False

    def _try_resume_after_restart(self, restart_song: dict[str, Any] | None,
                                  restart_req_id: int | None = None):
        """重启后恢复播放（用重启时点快照）：本地文件重新 loadfile（从头播）；
        在线 URL 不恢复（源已断），把 _last_idle 置 False 产生上升沿自动切歌。
        restart_req_id：重启时点捕获的播放请求代次——当前代次已变化
        （重启窗口内用户切歌/发起新播放）→ 旧快照作废，绝不恢复旧歌。
        校验与恢复播放同处一个临界区（self._lock）：用户新意图若在线性化顺序上
        先于恢复操作（已获锁递增代次），恢复在获锁后校验必然失败；若恢复先获锁，
        则恢复完成后用户新意图再获锁播放——两种顺序最终都由用户新意图接管，
        旧快照不可能覆盖新选择（修复校验通过后、重放前存在插入窗口的竞态）。"""
        if not restart_song:
            return
        # 模块已停止 → 不恢复旧歌（重启 worker 可能在 stop() 后才完成重启）。
        if not self._running:
            return
        with self._lock:
            if restart_req_id is not None and restart_req_id != self._play_request_id:
                logger.info("[Music] 重启窗口内已发起新播放（代次变化），放弃恢复旧歌")
                return
            path = restart_song.get("path") or ""
            if path.startswith(("http://", "https://")):
                self._last_idle = False
                return
            self._play_song(restart_song)

    def stop(self):
        """停止音乐模块"""
        # 决策点：模块级 stop 被触发（记录调用来源；controller 内部无 self.stop() 调用，
        # 均由外部/UI 触发，caller 恒为 stop 方法本身）
        self._record_decision({
            "kind": "module_stop",
            "caller": "stop()",
        })
        self._running = False
        self.cancel_sleep_timer()
        self._invalidate_highlight()
        stop_scanner = getattr(getattr(self, "scanner", None), "stop_highlight_background", None)
        if callable(stop_scanner) and stop_scanner() is False:
            logger.warning("[Music] 已请求停止后台 highlight 任务，但尚未确认线程退出")
        if getattr(self, "online_manager", None) is not None:
            try:
                self.online_manager.close()
            except Exception:
                pass
        self.mpv.stop_process()
        logger.info("[Music] 模块已停止")

    def _register_commands(self):
        """注册命令处理器"""
        self.command_bus.register(CommandType.MUSIC_PLAY, lambda c: self.play())
        self.command_bus.register(CommandType.MUSIC_PAUSE, lambda c: self.pause())
        self.command_bus.register(CommandType.MUSIC_RESUME, lambda c: self.resume())
        self.command_bus.register(CommandType.MUSIC_TOGGLE, lambda c: self.toggle())
        self.command_bus.register(CommandType.MUSIC_NEXT, lambda c: self.next())
        self.command_bus.register(CommandType.MUSIC_PREV, lambda c: self.prev())
        self.command_bus.register(CommandType.MUSIC_VOLUME_SET, self._cmd_volume_set)
        self.command_bus.register(CommandType.MUSIC_VOLUME_UP, lambda c: self.volume_up(c.get("step", 10)))
        self.command_bus.register(CommandType.MUSIC_VOLUME_DOWN, lambda c: self.volume_down(c.get("step", 10)))
        self.command_bus.register(CommandType.MUSIC_SEEK, self._cmd_seek)
        self.command_bus.register(CommandType.MUSIC_SEARCH_PLAY, self._cmd_search_play)
        self.command_bus.register(CommandType.MUSIC_HIGHLIGHT, lambda c: self.jump_to_highlight())
        self.command_bus.register(CommandType.MUSIC_HIGHLIGHT_MODE, self._cmd_highlight_mode)
        self.command_bus.register(CommandType.MUSIC_SET_PLAY_MODE, self._cmd_set_play_mode)
        self.command_bus.register(CommandType.MUSIC_SET_SPEED, lambda c: self.set_speed(c.params.get("speed")) if "speed" in c.params else False)
        self.command_bus.register(CommandType.MUSIC_PLAY_PLAYLIST, self._cmd_play_playlist)
        self.command_bus.register(CommandType.MUSIC_CREATE_PLAYLIST, self._cmd_create_playlist)
        self.command_bus.register(CommandType.MUSIC_ADD_TO_PLAYLIST, self._cmd_add_to_playlist)
        self.command_bus.register(CommandType.MUSIC_ADD_NEXT, self._cmd_add_next)
        self.command_bus.register(CommandType.MUSIC_PLAY_RANK_INDEX, self._cmd_play_rank_index)
        self.command_bus.register(CommandType.MUSIC_RESCAN, lambda c: self.rescan())
        self.command_bus.register(CommandType.MUSIC_CLEANUP, lambda c: self.cleanup_missing())
        self.command_bus.register(CommandType.MUSIC_CLEAR_QUEUE, lambda c: self.clear_queue())
        self.command_bus.register(CommandType.MUSIC_STOP, lambda c: self.stop_current())
        self.command_bus.register(CommandType.MUSIC_SLEEP_TIMER_SET,
                                  lambda c: self.set_sleep_timer(c.params.get("seconds")))
        self.command_bus.register(CommandType.MUSIC_SLEEP_TIMER_CANCEL,
                                  lambda c: self.cancel_sleep_timer())

    # ===== 播放控制 =====

    @_cancel_switch_on_user_action
    def play(self):
        """播放当前队列；队列为空时从本地库随机选择一首。"""
        # 打点：意图入口（不持锁，lock_wait_ms 记为 0）
        self._record_intent("play", self._current_song, 0)
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return False
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.play():
                    # 落雪播放同样算"音乐开始播放"→ 通知消费方（电影互斥暂停）
                    self.event_bus.publish(Event(
                        event_type=EventType.MUSIC_PLAY,
                        data={},
                        source="music",
                    ))
                    return True
            except Exception as e:
                logger.error(f"[Music] 落雪 play 失败，回落 mpv: {e}")
                self.lx_connected = False
        
        if self.queue.is_empty():
            # 队列空，随机播放
            songs = self.db.get_random_songs(20)
            if songs:
                # 与 play_song/next/prev 同契约：_play_song 播放副作用持同一业务锁，
                # 防止并发 pause 被本路径的起播 unpause 抹掉（起播即响竞态）。
                with self._lock:
                    self.queue.add_all(songs)
                    self._play_song(self.queue.next())
                return True
            return False
        else:
            # 检查 mpv 是否有文件加载（stop/eof 后没有文件，pause=false 不起作用）
            try:
                is_idle = self.mpv.is_idle()
                has_file = bool(self.mpv.get_filename())
            except Exception:
                is_idle = True
                has_file = False

            if is_idle or not has_file:
                # 没有文件，重新加载当前歌曲
                current = self.queue.get_current()
                if current:
                    with self._lock:
                        self._play_song(current)
                    return True
                else:
                    song = self.queue.next()
                    if song:
                        with self._lock:
                            self._play_song(song)
                        return True
                    return False
            else:
                # 恢复播放与起播同类副作用（unpause），与 pause 等持锁操作互斥。
                with self._lock:
                    self.mpv.play()
                    self._update_state(playing=True)
                    # 继续播放同一首歌 → 通知其他消费方（电影互斥暂停）；换歌路径由 TRACK_CHANGED 覆盖
                    self.event_bus.publish(Event(
                        event_type=EventType.MUSIC_PLAY,
                        data={},
                        source="music",
                    ))
                return True

    @_cancel_switch_on_user_action
    def pause(self):
        """暂停"""
        # 打点：意图入口（不持锁，lock_wait_ms 记为 0）
        self._record_intent("pause", self._current_song, 0)
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.pause():
                    return
            except Exception as e:
                logger.error(f"[Music] 落雪 pause 失败，回落 mpv: {e}")
                self.lx_connected = False
        
        # 暂停/恢复与播放/切歌共用同一业务锁：pause 要么先于切歌生效（作用于旧歌），
        # 要么阻塞等锁、在切歌（loadfile + 无条件 unpause）完成后才执行（作用于新歌）。
        # 二者必居其一——切歌进行中提出的暂停意图绝不会被 _play_song 的
        # set_property("pause", False) 无条件抹掉（起播即响竞态修复）。
        with self._lock:
            self.mpv.pause()
            self._paused_snapshot = True
            # 实时同步状态
            try:
                self.mpv._properties["pause"] = True
            except Exception:
                pass
            self._update_state(playing=False)

    @_cancel_switch_on_user_action
    def resume(self):
        """继续播放"""
        # 打点：意图入口（不持锁，lock_wait_ms 记为 0）
        self._record_intent("resume", self._current_song, 0)
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return
        # 与 pause 同契约：与播放/切歌持同一业务锁，恢复意图与切歌顺序化，
        # 不会与 _play_song 的 loadfile/unpause 交错（切歌完成后的恢复作用于新歌）。
        with self._lock:
            self.mpv.play()
            self._paused_snapshot = False
            # 实时同步状态
            try:
                self.mpv._properties["pause"] = False
            except Exception:
                pass
            self._update_state(playing=True)
            # 继续播放 → 通知其他消费方（电影互斥暂停）
            self.event_bus.publish(Event(
                event_type=EventType.MUSIC_PLAY,
                data={},
                source="music",
            ))

    @_cancel_switch_on_user_action
    def toggle(self):
        """切换播放/暂停（直接让 mpv 处理，不依赖缓存状态）"""
        # 打点：意图入口（不持锁，lock_wait_ms 记为 0）
        self._record_intent("toggle", self._current_song, 0)
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.toggle_play():
                    return
            except Exception as e:
                logger.error(f"[Music] 落雪 toggle 失败，回落 mpv: {e}")
                self.lx_connected = False
        
        # 与 pause/resume 同契约：切换与播放/切歌持同一业务锁，toggle 的
        # toggle_pause + 读回与 _play_song 的 loadfile/unpause 互斥——切歌窗口内
        # 的 toggle 排队等锁后作用于新歌，不会在 loadfile 前被无条件 unpause 吞掉。
        with self._lock:
            self.mpv.toggle_pause()
            # 同步更新状态
            try:
                paused = self.mpv.get_property("pause", timeout=1.0)
                self._paused_snapshot = bool(paused)
                self._update_state(playing=(paused is False))
            except Exception:
                pass

    @_cancel_switch_on_user_action
    def next(self, _timeout: float | None = None):
        """下一首。

        _timeout 非空时：业务锁忙则放弃本轮（自动跳过路径用，防无限等锁）。
        """
        # 打点：意图入口（t_enter 仅在 trace 启用时计时；实际记录在锁内）
        _t_enter = time.perf_counter() if (self._trace and self._trace.enabled) else None
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return
        # 防抖：语音 + UI 并发双触发（<300ms）只跳一首，避免一次跳两首
        now = time.time()
        if now - self._last_next_ts < 0.3:
            logger.info("[Music] next 防抖：300ms 内重复触发已忽略")
            return
        self._last_next_ts = now
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.next():
                    return
            except Exception as e:
                logger.error(f"[Music] 落雪 next 失败，回落 mpv: {e}")
                self.lx_connected = False

        if _timeout is not None:
            if not self._lock.acquire(timeout=_timeout):
                logger.info("[Music] next：业务锁忙，放弃本轮")
                return
            try:
                self._next_locked()
            finally:
                self._lock.release()
            return

        with self._lock:
            # 意图打点：已获取锁，记录锁等待耗时与切走前的当前曲目
            if _t_enter is not None:
                self._record_intent(
                    "next", self.queue.get_current(),
                    round((time.perf_counter() - _t_enter) * 1000, 2))
            self._next_locked()

    def _next_locked(self):
        """next() 的锁内逻辑（持锁调用）。"""
        # 自动切歌守卫：origin=auto 且 _last_idle=True → 停止操作（stop_current/模块 stop）
        # 已先置位完成，先前捕获的 idle 上升沿作废，不得启动下一首。
        # 判定位于 next() 的业务锁内，与 stop_current 的持锁置位互斥，
        # 形成可靠同步边界（threading.Lock 不可重入，守卫放锁内而非再取锁）。
        if self._current_origin() == "auto" and getattr(self, "_last_idle", True):
            logger.info("[Music] auto_next 拦截：停止已生效，不作废旧上升沿")
            return
        self._invalidate_highlight()
        is_auto = (self._current_origin() == "auto")
        song = self.queue.next(auto=is_auto)
        if song:
            self._play_song(song)
        else:
            # 队列空（顺序模式播完队列尾）：停止当前播放、保持空闲，
            # 绝不跨界加载本地库——在线歌单播完不得突然切到本地歌曲
            # （旧逻辑 db.get_all_songs 自动加载整个本地库续播，是 bug 根源）。
            # 只 mpv.stop() 回 idle（同缺失文件 3 次保护），mpv 实例与轮询线程继续存活。
            self._current_song = None
            self._last_idle = True
            try:
                self.mpv.stop()
            except Exception:
                pass
            self._update_state(playing=False, current_song=None)

    @_cancel_switch_on_user_action
    def prev(self):
        """上一首"""
        # 打点：意图入口（实际记录在锁内）
        _t_enter = time.perf_counter() if (self._trace and self._trace.enabled) else None
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.prev():
                    return
            except Exception as e:
                logger.error(f"[Music] 落雪 prev 失败，回落 mpv: {e}")
                self.lx_connected = False
        
        with self._lock:
            # 意图打点：已获取锁
            if _t_enter is not None:
                self._record_intent(
                    "prev", self.queue.get_current(),
                    round((time.perf_counter() - _t_enter) * 1000, 2))
            self._invalidate_highlight()
            song = self.queue.prev()
            if song:
                self._play_song(song)

    @_cancel_switch_on_user_action
    def play_song(self, song: dict[str, Any]):
        """播放指定歌曲"""
        # 打点：意图入口（实际记录在锁内）
        _t_enter = time.perf_counter() if (self._trace and self._trace.enabled) else None
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return
        # 代际号由 _play_song（唯一咽喉）统一递增，此处不再重复
        with self._lock:
            # 意图打点：已获取锁
            if _t_enter is not None:
                self._record_intent(
                    "play_song", song,
                    round((time.perf_counter() - _t_enter) * 1000, 2))
            if self._mpv_restart_pending:
                logger.info("[Music] mpv 重启中，忽略播放请求")
                return
            self._invalidate_highlight()
            # 如果队列是空的（语音首次播放本地歌曲），自动把整个本地库加进队列
            if self.queue.size() == 0 and not song.get("online"):
                try:
                    all_songs = self.db.get_all_songs(limit=1000)
                    if all_songs:
                        self.queue.add_all(all_songs)
                except Exception as e:
                    logger.error(f"加载本地队列失败: {e}")
            # 添加到队列并播放
            self.queue.add(song)
            self.queue.play_at(self.queue.size() - 1)
            self._play_song(song)

    def get_history(self, limit: int = 200) -> list:
        """播放历史（最近在前）。封面补全：本地反查 path，在线查最近封面缓存。"""
        try:
            rows = self.db.get_history(limit=limit)
            return [self._enrich_cover(dict(r)) for r in rows]
        except Exception as e:
            logger.error(f"[Music] get_history error: {e}")
            return []

    # ---- 历史/歌单封面补全（本地反查 path / 在线查最近封面缓存） ----
    _cover_mem: dict = {}   # 在线封面内存缓存："{title}|{artist}" / "{source}|{track_id}" -> url

    def remember_cover(self, title: str = "", artist: str = "",
                       source_name: str = "", track_id: str = "",
                       cover_url: str = "") -> None:
        """在线歌渲染/播放时登记封面，供历史与歌单缺字段时补全。"""
        if not cover_url:
            return
        if title or artist:
            self._cover_mem[f"{title or ''}|{artist or ''}"] = cover_url
        if source_name and track_id:
            self._cover_mem[f"{source_name}|{track_id}"] = cover_url

    def _enrich_cover(self, row: dict) -> dict:
        """单行补封面：已有 cover_url/path 直接返回；本地按 music_id/title 反查
        path（Delegate 走 ID3/同目录封面）；在线查最近封面缓存。"""
        if not isinstance(row, dict):
            return row
        if row.get("cover_url") or row.get("path"):
            return row
        st = row.get("source_type", "")
        try:
            if st == "local":
                mid = int(row.get("music_id") or 0)
                song = self.db.get_song_by_id(mid) if mid > 0 else None
                if song and song.get("path"):
                    row["path"] = song["path"]
                    return row
                q = " ".join(x for x in (row.get("title"), row.get("artist")) if x)
                for hit in self.db.search(q, limit=5):
                    if hit.get("path"):
                        row["path"] = hit["path"]
                        return row
            else:
                url = (self._cover_mem.get(f"{row.get('title')}|{row.get('artist')}")
                       or self._cover_mem.get(
                           f"{row.get('source_name')}|{row.get('track_id')}"))
                if url:
                    row["cover_url"] = url
        except Exception as e:
            logger.error(f"[Music] enrich cover error: {e}")
        return row

    def lookup_cover(self, row: dict) -> dict:
        """面板侧兜底：返回补全后的行（不修改原字典）。"""
        return self._enrich_cover(dict(row))

    def clear_history(self):
        """清空播放历史。"""
        try:
            self.db.clear_history()
        except Exception as e:
            logger.error(f"[Music] clear_history error: {e}")

    def play_history_item(self, item: dict) -> bool:
        """历史重播：本地按 music_id 找歌；在线按 歌名+歌手 重搜。失败返回 False。"""
        if not item:
            return False
        try:
            music_id = int(item.get("music_id") or 0)
            if music_id > 0:
                song = self.db.get_song_by_id(music_id)
                if song and song.get("path") and os.path.exists(song.get("path", "")):
                    self.play_song(song)
                    return True
                return False
            q = " ".join(x for x in (item.get("title"), item.get("artist")) if x).strip()
            if not q:
                return False
            r = self.play_search_result(q, 0)
            return r is not None
        except Exception as e:
            logger.error(f"[Music] play_history_item error: {e}")
            return False

    # ---- 歌单 ----

    def create_playlist(self, name: str):
        """新建歌单；重名返回 None。"""
        try:
            return self.db.create_playlist(name)
        except Exception as e:
            logger.error(f"[Music] create_playlist error: {e}")
            return None

    def delete_playlist(self, playlist_id: int):
        """删除歌单（级联删歌单内歌曲）。"""
        try:
            self.db.delete_playlist(playlist_id)
        except Exception as e:
            logger.error(f"[Music] delete_playlist error: {e}")

    def rename_playlist(self, playlist_id: int, name: str) -> bool:
        """重命名歌单；重名返回 False。"""
        try:
            return self.db.rename_playlist(playlist_id, name)
        except Exception as e:
            logger.error(f"[Music] rename_playlist error: {e}")
            return False

    def get_playlists(self) -> list:
        """全部歌单（附 song_count）。"""
        try:
            return self.db.get_playlists()
        except Exception as e:
            logger.error(f"[Music] get_playlists error: {e}")
            return []

    def add_to_playlist(self, playlist_id: int, song: dict) -> bool:
        """加歌到歌单；同歌单内同歌去重（重复返回 False）。"""
        try:
            return self.db.add_to_playlist(playlist_id, song)
        except Exception as e:
            logger.error(f"[Music] add_to_playlist error: {e}")
            return False

    def remove_from_playlist(self, playlist_id: int, song_id: int) -> bool:
        """从歌单移除（song_id = playlist_songs.id）。"""
        try:
            return self.db.remove_from_playlist(playlist_id, song_id)
        except Exception as e:
            logger.error(f"[Music] remove_from_playlist error: {e}")
            return False

    def get_playlist_songs(self, playlist_id: int) -> list:
        """歌单内歌曲（按加入时间）。封面补全：本地反查 path，在线查最近封面缓存。"""
        try:
            rows = self.db.get_playlist_songs(playlist_id)
            return [self._enrich_cover(dict(r)) for r in rows]
        except Exception as e:
            logger.error(f"[Music] get_playlist_songs error: {e}")
            return []

    def play_playlist_item(self, item: dict) -> bool:
        """歌单播放：本地按 music_id 找歌；在线已下载优先播本地；否则在线重搜。失败返回 False。"""
        if not item:
            return False
        try:
            music_id = int(item.get("music_id") or 0)
            if music_id > 0:
                song = self.db.get_song_by_id(music_id)
                if song and song.get("path") and os.path.exists(song.get("path", "")):
                    self.play_song(song)
                    return True
                return False
            # 在线歌：已下载 → 优先播本地副本（下载时已入 music 表，按 title+artist 定位）
            if self.downloader is not None:
                try:
                    p = self.downloader.find_download_path(
                        item.get("title", ""), item.get("artist", ""))
                    if p and os.path.exists(p):
                        row2 = self.db.get_song_by_path(p)
                        if row2:
                            self.play_song(row2)
                            return True
                except Exception as e:
                    logger.error(f"[Music] 已下载歌优先本地失败: {e}")
            q = " ".join(x for x in (item.get("title"), item.get("artist")) if x).strip()
            if not q:
                return False
            r = self.play_search_result(q, 0)
            return r is not None
        except Exception as e:
            logger.error(f"[Music] play_playlist_item error: {e}")
            return False

    def get_song_path(self, music_id: int) -> str:
        """本地歌磁盘路径（文件不存在返回空串）。"""
        try:
            row = self.db.get_song_by_id(int(music_id))
            if row and row.get("path") and os.path.exists(row["path"]):
                return row["path"]
        except Exception as e:
            logger.error(f"[Music] get_song_path error: {e}")
        return ""

    def add_next(self, song: dict) -> bool:
        """下一首播放：跨模式优先，连续请求后选先播；无播放时插队首。"""
        try:
            self.queue.add_next(song)
            return True
        except Exception as e:
            logger.error(f"[Music] add_next error: {e}")
            return False

    def delete_local_file(self, song: dict) -> tuple:
        """删除本地文件（不可逆）：删磁盘 + 从 music 表移除（FTS 同步）+ 从歌单移除（历史保留）。
        返回 (ok, err_msg)。"""
        music_id = int(song.get("music_id") or song.get("id") or 0)
        if music_id <= 0:
            return False, "无效歌曲"
        try:
            row = self.db.get_song_by_id(music_id)
            if not row or not row.get("path"):
                return False, "歌曲记录不存在"
            path = row["path"]
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    return False, f"删除文件失败: {e!r}"
            self.db.delete_song(music_id)
            self.db.delete_playlist_songs_by_music_id(music_id)
            return True, ""
        except Exception as e:
            logger.error(f"[Music] delete_local_file error: {e}")
            return False, f"删除失败: {e!r}"

    def _cancel_candidate_switch(self):
        """取消进行中的候选换源（用户操作优先）。"""
        self._candidate_switch_token += 1
        self._candidate_switching = False

    def _publish_candidates(self, query: str, candidates: list) -> None:
        """P1 候选旁路：把候选推给 UI（事件只带展示 + 代际号）。

        track 本体留 self._active_candidates，UI 点击时通过
        play_online_resume(generation, idx) 回查——避免 OnlineTrack
        dataclass 进 EventLog 触发 JSON 序列化错误。
        """
        self._candidate_generation += 1
        gen = self._candidate_generation
        self._active_candidates = list(candidates)
        display = [
            {"idx": i, "source": c.source_name, "title": c.title, "artist": c.artist,
             "cover_url": getattr(c, "cover_url", None) or None}
            for i, c in enumerate(candidates)
        ]
        self.event_bus.publish(Event(
            event_type=ONLINE_SEARCH_DONE,
            data={"query": query, "display": display, "generation": gen},
            source="music",
        ))

    # 泛化播放词：用户只说"播放歌曲/放首歌/播音乐"等，没有指定歌名。
    # 这些词不应被当作歌名去在线搜索，而应从本地库随机挑一首。
    _VAGUE_PLAY_WORDS = {
        "歌曲", "歌", "音乐", "首歌", "首音乐", "曲子",
        "来首", "放首", "播首", "听首", "来一首", "放一首", "播一首", "听一首",
        "随便", "随便放", "随便播", "随便听听", "随便来",
        "播放", "放", "播", "听", "继续", "继续播放",
    }

    def _is_vague_play_query(self, query: str) -> bool:
        """判断 query 是否是泛化播放指令（未指定具体歌名）。"""
        if not query:
            return True
        q = query.strip().strip("《》<>「」\"'“”‘’ ")
        if not q:
            return True
        if q in self._VAGUE_PLAY_WORDS:
            return True
        # 已知"动词前缀 + 泛化词"组合，如"来首歌/放首歌/播一首音乐/点首歌"。
        # 不做 endswith 模糊匹配，避免把"新歌/老歌/情歌"等真歌名误判为泛化。
        prefixes = ("来", "放", "播", "听", "点", "给我")
        for p in prefixes:
            for w in ("歌曲", "歌", "音乐", "首歌", "首音乐"):
                if q == p + w or q == p + "一" + w:
                    return True
        return False

    def _play_vague_local(self):
        """泛化播放指令：从本地库随机挑一首播放；本地空则明确提示，不走在线兜底。"""
        try:
            all_songs = self.db.get_all_songs(limit=1000)
        except Exception as e:
            logger.error(f"[Music] 读取本地歌曲库失败: {e}")
            return None
        if not all_songs:
            logger.info("[Music] 本地歌曲库为空，无法泛化播放；请先扫描音乐目录或明确指定歌名")
            return None
        import random as _rnd
        song = _rnd.choice(all_songs)
        logger.info(f"[Music] 泛化播放指令 → 本地随机: {song.get('title','?')} - {song.get('artist','')}")
        self.play_song(song)
        return song

    @_cancel_switch_on_user_action
    def play_search_result(self, query: str, index: int = 0):
        """搜索并播放第 N 个结果，本地找不到自动走在线fallback。

        若 query 是泛化播放词（"歌曲/音乐/来首歌"等），不把它当歌名搜索，
        直接从本地库随机挑一首；本地空则提示，绝不在线乱搜。
        """
        # 打点：意图入口（搜索路径不持锁，lock_wait_ms 记为 0）
        self._record_intent("play_search_result", self._current_song, 0,
                            extra={"query": str(query)[:20], "index": int(index)})
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return None
        # 泛化播放指令：不走本地搜索/在线兜底，直接本地随机
        if self._is_vague_play_query(query):
            return self._play_vague_local()
        # 代际号由 _play_song（唯一咽喉）统一递增；本地命中路径经 play_song 覆盖。
        results = self.search.search(query, limit=10)
        if results and 0 <= index < len(results):
            self.play_song(results[index])
            return results[index]

        # 本地搜索失败，尝试在线fallback
        if self.online_manager is not None:
            # 在线 fallback 不经 _play_song（直连 mpv），必须自行递增代际号，
            # 作废在途的在线解析 worker（防旧网络请求覆盖本 fallback 播放）。
            # 递增与快照在锁内完成（与 play_online/_play_song 同契约）：
            # 避免无锁 read-modify-write 与持锁递增并发时丢代次、旧请求误判有效。
            with self._lock:
                self._play_request_id += 1
                req_id = self._play_request_id
            _t_online_start = time.time()
            try:
                logger.info(f"[Music] 本地未找到'{query}'，尝试在线搜索...")
                online_dict = self.online_manager.search_all(query, limit_per_source=5)
                _t_search_ms = int((time.time() - _t_online_start) * 1000)
                # 该行耗时/源列表不再打印到控制台（用户要求保持控制台简洁）
                # search_all返回{音源名: track列表}，展开成所有track
                all_tracks = []
                for src_name, tracks in online_dict.items():
                    all_tracks.extend(tracks)
                if all_tracks:
                    # 选源：同一首歌多源时，音质最优 → 同音质选最快（替代旧的"酷我优先"；
                    # 同音质同耗时时酷我系仍优先兜底，验证过能播）。
                    # failover：候选列表逐个试 get_play_url，第一个拿到 URL 的胜出
                    # （某源该歌 VIP/档位限制拿不到 URL 时，自动试下一个源）。
                    candidates = _pick_candidates(all_tracks, self.online_manager)
                    # P1 候选旁路：推给 UI（自动播代码不动；异常不影响播放）
                    try:
                        self._publish_candidates(query, candidates)
                    except Exception as _e:
                        logger.error(f"[Music] 候选推送失败（不影响播放）: {_e}")
                    _t_url_start = time.time()
                    chosen, play_result = None, None
                    for cand in candidates:
                        play_result = self.online_manager.get_play_url(cand)
                        if play_result and play_result.get("url"):
                            chosen = cand
                            break

                    if chosen is None:
                        logger.error(f"[Music] 获取播放URL失败（{len(candidates)} 个候选均无 URL），无法在线播放")
                        return None

                    logger.info(f"[Music] 在线找到: {chosen.title} - {chosen.artist} (音源: {chosen.source_name})")
                    # 定向取 URL：只调 chosen 对应的音源（manager 按 track.source_name 路由）。
                    # 旧逻辑遍历所有源串行调 get_play_url，demo/archive 对陌生 track 各自
                    # 发起 HTTP（超时 8s/个），导致在线播放最高 ~16s 延迟。
                    _t_url_ms = int((time.time() - _t_url_start) * 1000)
                    logger.info(f"[Music] 获取播放URL，耗时 {_t_url_ms}ms")
                    if play_result:
                        logger.info("[Music] 获取播放URL成功，开始在线播放...")
                        play_url = play_result.get("url")
                        # 统一流设置（header 重设 + loadfile + pause + 状态落库，DRY）。
                        # 慢 get_play_url 在锁外；返回后锁内权威校验代次（与
                        # play_online/_resolve_online_worker 同契约）——期间用户新
                        # 意图已获锁递增代次 → 校验失败，旧 URL 绝不 loadfile。
                        with self._lock:
                            if req_id != self._play_request_id:
                                logger.info("[Music] 在线 fallback 已过期（期间已切歌），丢弃结果")
                                return None
                            online_song = self._apply_stream(
                                chosen, play_url, play_result.get("headers"))
                        self.event_bus.publish(Event(
                            event_type=EventType.MUSIC_TRACK_CHANGED,
                            data={"song": online_song},
                            source="music"
                        ))
                        _t_total_ms = int((time.time() - _t_online_start) * 1000)
                        logger.info(f"[Music] 在线播放就绪，总耗时 {_t_total_ms}ms（搜索{_t_search_ms}ms + URL{_t_url_ms}ms）")
                        return online_song
                    else:
                        # 定向/兜底取 URL 均失败：明确提示，避免静默不播放
                        logger.error(f"[Music] 获取播放URL失败（音源: {chosen.source_name}），无法在线播放")
            except Exception as e:
                logger.error(f"[Music] 在线搜索失败: {e}")

        return None

    def _play_online_queue_item(self, song: dict[str, Any]):
        """队列在线歌播放：网络解析（重搜+取URL）放后台线程，不阻塞调用线程。
        解析完成后在同一 worker 线程执行 loadfile（mpv IPC 有锁、db 有锁、
        状态只写 state_store——轮询/Timer 线程早已多线程调用，安全）。
        快速连切时用 request_id 代际号丢弃过期 resolve，防止旧 URL 覆盖新歌。
        失败 → 延迟 next() 主动跳过：轮询上升沿会被 _last_idle=now_idle 覆盖吞掉
        （_play_song 的 reset 在 auto_next 同步返回后被 L295 覆盖），必须主动跳，
        不能等轮询。不发 MUSIC_TRACK_CHANGED（rule_engine/speech_judge 语音订阅者），
        直接 record_song 记历史。"""
        query = f"{song.get('title', '')} {song.get('artist', '')}".strip()
        if not query:
            logger.info("[Music] 队列在线歌无 title，跳过")
            self._schedule_skip_online(song)
            return
        # 代际号已由 _play_song（唯一咽喉）递增——此处只取当前值传给 worker
        req_id = self._play_request_id
        threading.Thread(
            target=self._resolve_online_worker,
            args=(song, query, req_id), daemon=True,
            name="music-online-resolve").start()

    def _resolve_online_worker(self, song: dict[str, Any], query: str, req_id: int):
        """后台线程：重搜 → 逐个候选取 URL → loadfile → 状态/历史。
        解析完成时 req_id 已过期（期间用户又切歌）→ 丢弃结果，不覆盖新歌。
        网络解析经 OnlineResolver（纯解析，不碰 mpv/队列/Qt 信号）；
        loadfile 必须在 controller._lock 内做二次校验后执行，防止与本地
        播放（play_song 持锁）并发竞态——旧网络请求绝不覆盖最新播放意图。"""
        try:
            if self._resolver is None:
                logger.error("[Music] 在线解析器未就绪，跳过")
                self._schedule_skip_online(song)
                return
            # 直取优先：榜单歌带完整 source_name/track_id/extra._raw 时，直接消费
            # ID 取流（js_source.get_play_url + bridge getUrl 原生支持），避免重搜
            # 命中片段版/试听版与音源错乱；直取失败才降级重搜兜底。
            url = None
            headers = None
            chosen_track = None
            play_result = None
            track_id = song.get("track_id")
            source_name = song.get("source_name")
            if track_id and source_name:
                try:
                    from app.music.online.models import OnlineTrack
                    direct_track = OnlineTrack(
                        source_name=source_name,
                        track_id=str(track_id),
                        title=song.get("title", "") or "",
                        artist=song.get("artist", "") or "",
                        album=song.get("album", "") or "",
                        duration_seconds=int(song.get("duration") or 0),
                        extra=song.get("extra") or {},
                    )
                    play_res = self.online_manager.get_play_url(
                        direct_track, quality="high")
                    if play_res and play_res.get("url"):
                        url = play_res["url"]
                        headers = play_res.get("headers")
                        chosen_track = direct_track
                        play_result = play_res
                        logger.info(
                            f"[Music] 队列在线歌直取成功: {direct_track.title} "
                            f"- {direct_track.artist} ({source_name})")
                except Exception as e:
                    logger.warning(f"[Music] 队列在线歌直取失败，准备重搜: {e!r}")
            if not url:
                # 降级重搜兜底
                logger.info(f"[Music] 队列在线歌走重搜兜底: {query}")
                result = self._resolver.resolve(query, limit_per_source=5)
                if not result:
                    logger.info("[Music] 队列在线歌重搜无结果/URL 均失败，跳过")
                    self._schedule_skip_online(song)
                    return
                chosen_track = result["track"]
                url = result["url"]
                play_result = result["play_result"]
            # 无锁快速检查：已过期直接放弃（省一次 IPC）
            if req_id != self._play_request_id:
                logger.info("[Music] 在线解析已过期（期间已切歌），丢弃结果")
                return
            # 锁内权威校验 + 播放：校验失败绝不 loadfile；
            # 锁内执行保证与 play_song（本地持锁）互斥，杜绝旧请求覆盖新歌
            with self._lock:
                if req_id != self._play_request_id:
                    logger.info("[Music] 在线解析已过期（期间已切歌），锁内丢弃结果")
                    return
                online_song = self._apply_stream(
                    chosen_track, url,
                    headers if headers is not None else play_result.get("headers"))
            try:
                self.history_recorder.record_song({
                    "id": -1,
                    "title": chosen_track.title,
                    "artist": chosen_track.artist,
                    "source": "online",
                    "source_name": chosen_track.source_name,
                    "track_id": chosen_track.track_id,
                })
            except Exception as e:
                logger.error(f"[Music] 记录播放历史失败: {e}")
        except Exception as e:
            logger.error(f"[Music] 队列在线歌播放失败: {e}")
            self._schedule_skip_online(song)

    def _apply_stream(self, track, url: str, headers, before_load=None) -> dict | None:
        """统一播放在线流：http-header-fields 重设 + loadfile + pause 同步 + 状态落库。

        三处在线播放共用（play_search_result 在线 fallback / 队列在线 worker /
        play_online），消除 header 设置与 online_song 构造的重复（DRY）。
        before_load: 可选校验回调——返回 False 则放弃播放（满足 play_online
        的"校验紧贴 loadfile"原子需求，校验与 loadfile 之间无耗时 IPC）。
        返回构造好的 online_song（_current_song/_update_state 已更新）；放弃返回 None。
        """
        hdr = _pick_play_headers(headers)
        src_name = (getattr(track, "source_name", "") or "").lower()
        need_kw_header = ("kw" in src_name or "酷我" in src_name
                          or "kuwo" in (url or "").lower())
        req_epoch = self._play_request_id  # 锁内快照：调用方已在锁内校验代次
        try:
            # mpv 的 http-header-fields 是持久属性：每次播放前无条件重设，
            # 避免某源 Referer 跨源残留（覆盖式 set_property）。
            # 优先级：插件返回 headers（白名单过滤） > source_name 硬编码（酷我）。
            self.mpv.set_property("http-header-fields", "")
            if hdr:
                fields = ",".join(f"{k}: {v}" for k, v in hdr.items())
                self.mpv.set_property("http-header-fields", fields)
            elif need_kw_header:
                self.mpv.set_property(
                    "http-header-fields",
                    "Referer: https://www.kuwo.cn/,User-Agent: Mozilla/5.0")
            else:
                self.mpv.set_property(
                    "http-header-fields",
                    "User-Agent: Mozilla/5.0")
        except Exception:
            pass
        if before_load is not None and not before_load():
            return None
        # 互斥：在线起播前确认电影已暂停（未装配 pauser 时阻断）
        if self._movie_pauser is None:
            logger.warning("[Music] movie pauser not wired, blocking online play")
            return None
        try:
            ok = self._movie_pauser()
            if not ok:
                logger.warning("[Music] 电影暂停确认失败，阻断在线起播")
                return None
        except Exception as e:
            logger.error(f"[Music] 电影暂停确认异常: {e}")
            return None
        # 影子模式：记录在线 loadfile 发起时刻与文件类型（classify 的 elapsed/kind 依据）
        self._shadow_loadfile_ts = time.perf_counter()
        self._shadow_file_kind = "online"
        try:
            resp = self.mpv.loadfile(url)
        except Exception as e:
            # loadfile 抛异常（IPC 未连接/写入失败/MPV 拒绝）：不得让切换态
            # 永久卡 True（UI 进度冻结），也不得伪造播放成功。
            logger.error(f"[Music] 在线 loadfile 失败: {url[:80]!r}: {e!r}")
            # 复位守卫：仅当本请求仍是最新播放意图才结束切换态——过期请求
            # （异常窗口内代次已被更新请求递增）不得覆盖新请求的状态。
            if self._play_request_id == req_epoch:
                self._is_switching_track = False
                self._last_idle = False
            # 复用调用方既有异常处理（play_online/在线 fallback/队列 worker
            # 均有 except 兜底），不在本层伪造成功或改动队列/历史/当前曲目。
            raise
        if resp is None:
            # 结果未确认（IPC 超时/无响应）：不得写入成功播放状态，按既有
            # 失败语义收口切换态（同样带代次守卫），并 raise 交由调用方既有
            # except 走失败/跳过路径——绝不把未确认当作成功。
            logger.error(f"[Music] 在线 loadfile 结果未确认: {url[:80]!r}")
            if self._play_request_id == req_epoch:
                self._is_switching_track = False
                self._last_idle = False
            raise RuntimeError("在线 loadfile 结果未确认（IPC 超时/无响应）")
        if not isinstance(resp, dict) or resp.get("error") != "success":
            # 协议层明确失败响应：命令未被接受，同样按失败收口并交由调用方
            # 既有 except 处理（不伪造播放成功）。
            err = resp.get("error") if isinstance(resp, dict) else resp
            logger.error(f"[Music] 在线 loadfile 失败: error={err}: {url[:80]!r}")
            if self._play_request_id == req_epoch:
                self._is_switching_track = False
                self._last_idle = False
            raise RuntimeError(f"在线 loadfile 失败: error={err}")
        # 在线歌已真正 loadfile：代次生效（_poll_idle 恢复写缓存）+ 结束切换态。
        # 调用方（_resolve_online_worker/play_online）已在锁内/入口校验过 req_id
        # 等于当前代次，此处 loadfile 的一定是最新播放意图。
        self._last_loadfile_epoch = self._play_request_id
        self._is_switching_track = False
        self.mpv.set_property("pause", False)
        try:
            self.mpv._properties["pause"] = False
        except Exception:
            pass
        online_song = {
            "id": -1,
            "title": getattr(track, "title", "") or "",
            "artist": getattr(track, "artist", "") or "",
            "album": getattr(track, "album", "") or "",
            "duration": getattr(track, 'duration_seconds', 0) or 0,
            "path": url,
            "source": "online",
            "source_name": getattr(track, 'source_name', '') or '',
            "track_id": getattr(track, 'track_id', '') or '',
            "extra": getattr(track, 'extra', None) or {},
        }
        self._current_song = online_song
        self._playback_gen += 1  # V2-2：在线流加载，会话代次递增
        self._user_stopped = False  # V2-4.5.6：新会话清除停止标记
        self._update_state(playing=True, current_song=online_song)
        return online_song

    def _schedule_skip_online(self, song: dict[str, Any]):
        """延迟 next() 跳过队列在线歌（不依赖轮询上升沿——会被 _last_idle 覆盖吞掉）。"""
        threading.Timer(0.2, lambda: self._do_skip_online(song)).start()

    def _do_skip_online(self, song: dict[str, Any]):
        """跳队列在线歌：队列 current 仍是该 song 才跳（防覆盖用户切歌操作）。"""
        # 模块已停止 → 自动跳过不得再操作 mpv（与 _auto_next 的 _running 守卫同源）。
        if not self._running:
            return
        # 跳曲前代次（本失败请求）；必须在 try 外捕获：守卫 return 也走 finally。
        req_before = self._play_request_id
        # origin 上下文：自动跳过标记为 auto（_play_song 内部据此区分意图）
        with self._origin_ctx("auto"):
            try:
                if self.queue.get_current() is not song:
                    return
                # 决策点：在线歌跳过真正触发
                self._record_decision({
                    "kind": "skip_online",
                    "reason": "queue_online_skip",
                })
                self.next(_timeout=0.5)
            except Exception as e:
                logger.info(f"[Music] 跳过队列在线歌 next() 异常: {e!r}")
            finally:
                # 空队列收口：跳曲未启动新播放请求（next 队列尾返回 None / 锁忙
                # 放弃 / next 早期异常）且代次仍属于本失败请求（无新意图接管）→
                # 收口遗留切换态（_next_locked 空队列分支不递增代次、不复位）。
                # 若 next 已推进并触发 _play_song（代次递增）或期间已有新请求
                # （用户切歌必递增代次），由对应请求生命周期收口，此处不得复位
                # ——防止旧跳曲覆盖新请求的切换态。
                with self._lock:
                    if self._play_request_id == req_before:
                        self._is_switching_track = False

    def _play_song(self, song: dict[str, Any]):
        """实际播放歌曲（唯一咽喉：play_song/next/prev/_auto_next/在线队列全部汇入）"""
        if not song:
            return
        # 播放请求代际号（request_id 契约）：一切新播放意图在此递增，作废在途的
        # 在线解析结果——旧网络请求（_resolve_online_worker）锁内校验不过，绝不 loadfile。
        self._play_request_id += 1
        current_epoch = self._play_request_id  # 本代次号（代次隔离：窗口内旧数据全部失效）
        # UI 切换态：新歌 loadfile 前禁止渲染旧进度（在线解析窗口内 mpv 仍报旧歌位置）
        self._is_switching_track = True
        # 切歌重置播放时间快照与暂停标志：防止旧位置外推污染新歌的进度显示
        self._playback_time_cache = (0.0, time.perf_counter(), False)
        self._paused_snapshot = False

        # 每次“尝试播放”都重置 idle 轮询状态，让轮询重新感知新歌：
        # - 正常：mpv 进入 playing，轮询读到 idle-active=False，与 _last_idle=False 一致；
        # - 文件损坏/不存在：mpv 仍（或又）处于 idle → 产生新的 False→True 上升沿 →
        #   自动切歌被再次触发，从而跳过坏文件继续播（否则 _last_idle 停留在 True，
        #   坏文件 loadfile 后永远没有上升沿，切歌卡死）。
        self._last_idle = False

        # 在线歌（右键"下一首播放"入队）：重搜 URL 播放，本地路径不涉及
        if song.get("source_type") == "online":
            self._play_online_queue_item(song)
            return

        filepath = song.get("path")
        if not filepath or not os.path.exists(filepath):
            # 决策点：缺失文件分支（记录连续失败计数）
            self._record_decision({
                "kind": "missing_file",
                "filepath_kind": "local",
                "fail_streak_before": self._missing_file_streak,
            })
            logger.info(f"[Music] 文件不存在: {filepath}")
            self._missing_file_streak += 1
            if self._missing_file_streak >= 3:
                # 限次保护：连续 3 次文件不可用 → 放弃播放，避免无限空转
                self._missing_file_streak = 0
                logger.info("[Music] 连续 3 次文件不可用，停止当前播放")
                # 关键：只"停止播放"（mpv 回 idle），绝不能用模块级 stop()——
                # stop() 会杀 mpv 进程并停掉轮询线程，整个音乐模块停摆。
                # 只是这首歌播不了，轮询线程与 mpv 实例必须继续存活。
                self._current_song = None
                self._last_idle = True
                try:
                    self.mpv.stop()
                except Exception:
                    pass
                self._update_state(playing=False, current_song=None)
                return
            # 单曲循环 + 文件不存在 → 跳出循环，顺序切下一首（防卡死）
            if self.queue.get_play_mode() == PlayMode.REPEAT_ONE:
                logger.info("[Music] 单曲循环文件不存在，顺序切下一首")
                self.queue.skip_current()
            # 防御性明确：本分支 return 前重置轮询状态（L673 已置过，
            # 保留以防御未来该行被移动后回归）
            self._last_idle = False
            # 切歌失败（文件缺失）：仅在"当前代次已真正 loadfile 过"时才结束切换态
            # （此时旧歌已停播、缓存已钉 0.0，UI 恢复渲染无污染）。
            # 否则（首次播放即失败 / 本轮尚未起播任何文件）保持切换态="加载中"，
            # 防止 mpv 仍在播旧歌时 UI 提前恢复渲染而露出旧进度（倒退）；
            # 后续由 confirm/auto_next 推进队列处理。
            if self._play_request_id == self._last_loadfile_epoch:
                self._is_switching_track = False
            return

        # 影子模式：记录 loadfile 发起时刻与文件类型（classify 的 elapsed/kind 依据）
        self._shadow_loadfile_ts = time.perf_counter()
        self._shadow_file_kind = "online" if str(filepath).startswith("http") else "local"

        try:
            resp = self.mpv.loadfile(filepath)
        except Exception as e:
            # loadfile 抛异常（IPC 未连接/写入失败/MPV 拒绝）：不得让切换态
            # 永久卡 True（UI 进度冻结），也不得伪造播放成功。
            logger.error(f"[Music] 本地 loadfile 失败: {filepath!r}: {e!r}")
            # 复位守卫：仅当本请求仍是最新播放意图才结束切换态——过期请求
            # （异常窗口内代次已被更新请求递增）不得覆盖新请求的状态。
            if self._play_request_id == current_epoch:
                self._is_switching_track = False
                self._last_idle = False
            # 不更新 _current_song/队列/播放历史；由轮询上升沿或用户操作推进
            # （与缺失文件分支同契约：坏文件交给 auto_next/用户处理）。
            return
        if resp is None:
            # 结果未确认（IPC 超时/无响应）：不得写入成功播放状态。与异常同款
            # 复位守卫收口切换态，交由轮询上升沿/用户操作推进（绝不伪造成功）。
            logger.error(f"[Music] 本地 loadfile 结果未确认: {filepath!r}")
            if self._play_request_id == current_epoch:
                self._is_switching_track = False
                self._last_idle = False
            return
        if not isinstance(resp, dict) or resp.get("error") != "success":
            # 协议层明确失败响应（命令未被接受）：同样按失败收口，不伪造成功。
            err = resp.get("error") if isinstance(resp, dict) else resp
            logger.error(f"[Music] 本地 loadfile 失败: error={err}: {filepath!r}")
            if self._play_request_id == current_epoch:
                self._is_switching_track = False
                self._last_idle = False
            return
        # 本地歌已真正 loadfile 起播：代次生效（_poll_idle 恢复写缓存）+ 结束切换态
        self._last_loadfile_epoch = self._play_request_id
        self._is_switching_track = False
        # 播放成功 → 重置连续失败计数
        self._missing_file_streak = 0
        # 确保新歌曲开始播放（不继承之前的暂停状态）
        self.mpv.set_property("pause", False)
        try:
            self.mpv._properties["pause"] = False
        except Exception:
            pass
        self._current_song = song
        self._playback_gen += 1  # V2-2：新媒体加载，会话代次递增
        self._user_stopped = False  # V2-4.5.6：新会话清除停止标记

        # 异步播放确认：坏文件（MPEG header junk 等）loadfile 返回 success 但
        # 解码失败——mpv 会立刻回到 idle，或卡在“播放中”却拿不到任何播放时间
        # （playback-time/duration 均为 None）。此时 idle 轮询无法形成上升沿，
        # 播放器会永久卡住。确认线程发现播放未真正开始 → 自动跳过切下一首。
        threading.Thread(
            target=self._confirm_playback,
            args=(song, filepath),
            daemon=True,
            name="music-playback-confirm"
        ).start()

        # 增加播放计数
        self.db.increment_play_count(song["id"])

        # 更新状态
        self._update_state(
            playing=True,
            current_song={
                "id": song.get("id"),
                "title": song.get("title"),
                "artist": song.get("artist"),
                "album": song.get("album"),
                "duration": song.get("duration"),
                "path": filepath,
            }
        )

        # 发布事件
        self.event_bus.publish(Event(
            event_type=EventType.MUSIC_TRACK_CHANGED,
            data={"song": song},
            source="music"
        ))

        # Highlight Mode：如果开启，seek 到 highlight start_time 并启动 poller
        if self._highlight_mode:
            self._start_highlight_segment(song)

    # ===== 坏文件自动跳过 =====

    def _confirm_playback(self, song: dict[str, Any], filepath: str):
        """loadfile 后异步确认播放是否真正开始。
        判定失败（mpv 仍 idle / 拿不到播放时间）→ 自动切下一首跳过坏文件。"""
        import time as _t
        _t.sleep(0.8)
        # 等待窗口内模块已停止（stop()）→ 不再查询/操作已停止的 mpv，直接退出。
        if not self._running:
            return
        try:
            idle_now = bool(self.mpv.get_property("idle-active", timeout=0.5))
            pt = self.mpv.get_property("playback-time", timeout=0.5)
            dur = self.mpv.get_property("duration", timeout=0.5)
            # 顺带更新播放时间快照（同一次后台确认的 IPC 会话内）
            if pt is not None:
                try:
                    self._playback_time_cache = (
                        float(pt), _t.perf_counter(), self._paused_snapshot)
                except Exception:
                    pass
        except Exception:
            idle_now, pt, dur = True, None, None
        # 期间用户已切走/正常播完 → 不干预
        if self._current_song is not song:
            return
        bad = idle_now or (pt is None and dur is None)
        if bad:
            # 坏文件自动跳过静默执行（不再打印到控制台）
            self._schedule_skip_bad(song)

    def _schedule_skip_bad(self, song: dict[str, Any]):
        """延后执行 next() 跳过坏文件（防重入 + 防覆盖用户操作）"""
        if not self._running:
            return
        if self._current_song is not song or self._skip_pending:
            return
        self._skip_pending = True
        threading.Timer(0.2, lambda: self._do_skip_bad(song)).start()

    def _do_skip_bad(self, song: dict[str, Any]):
        # 模块已停止 → 自动跳过不得再操作 mpv（与 _auto_next 的 _running 守卫同源）。
        if not self._running:
            return
        # origin 上下文：自动跳过标记为 auto（_play_song 内部据此区分意图）
        with self._origin_ctx("auto"):
            self._skip_pending = False
            if self._current_song is not song:
                return
            try:
                # 决策点：坏文件跳过真正触发
                self._record_decision({
                    "kind": "skip_bad",
                    "missing_file_streak": self._missing_file_streak,
                    "reason": "file_not_found",
                })
                # 抢锁 0.5s：锁忙（UI/重启持锁）则放弃本轮，下轮 confirm/轮询再处理
                self.next(_timeout=0.5)
            except Exception as e:
                logger.info(f"[Music] 跳过坏文件时 next() 异常: {e!r}")

    # ===== 音量控制 =====

    def set_online_manager(self, manager) -> None:
        self.online_manager = manager
        # 在线解析器随源就绪创建（纯网络解析，不碰 mpv/队列/Qt 信号）
        if self._resolver is None:
            self._resolver = OnlineResolver(manager)
        else:
            self._resolver.set_manager(manager)
        # 下载器随在线源就绪创建（不阻塞 UI、独立线程池，不碰本对象 mpv）
        if self.downloader is None:
            try:
                from app.music.downloader import DownloadManager
                self.downloader = DownloadManager(
                    manager, self.db, self.download_dir, max_workers=3)
            except Exception as e:
                logger.error(f"[Music] 下载器初始化失败: {e}")

    def download_song(self, song: dict[str, Any], callbacks=None) -> bool:
        """在线歌下载入队（薄代理）。callbacks: on_progress/on_done/on_fail/on_exists。"""
        if self.downloader is None:
            if callbacks and callbacks.get("on_fail"):
                key = f"{song.get('title', '')}|{song.get('artist', '')}".strip('|')
                callbacks["on_fail"](key, "下载服务未就绪")
            return False
        try:
            return self.downloader.download(song, callbacks)
        except Exception as e:
            logger.error(f"[Music] download_song error: {e}")
            return False

    def is_downloaded(self, title: str, artist: str) -> bool:
        """下载目录里已有 歌名 - 歌手.* → 已下载。"""
        if self.downloader is None:
            return False
        try:
            return self.downloader.is_downloaded(title, artist)
        except Exception:
            return False

    def remove_download(self, song: dict[str, Any]) -> tuple:
        """删除下载副本：删文件 + 从 music 表移除（历史保留）。返回 (ok, path, err)。"""
        if self.downloader is None:
            return False, "", "下载服务未就绪"
        try:
            ok, path, err = self.downloader.remove_download(
                song.get("title", ""), song.get("artist", ""))
        except Exception as e:
            return False, "", str(e)
        if ok and path:
            try:
                row = self.db.get_song_by_path(path)
                if row:
                    self.db.delete_song(row["id"])
            except Exception as e:
                logger.error(f"[Music] 移除下载记录失败: {e}")
        return ok, path, err

    def sync_custom_sources(self) -> int:
        """把 config/music_sources.json 中启用的自定义音源同步注册进 online_manager。

        幂等：先注销上次同步注册的 HTTP 源，再按当前启用列表重建。
        - 跳过 api_url 为空的源（如仅存元信息的 JS 导入）
        - 不覆盖 js_bridge / demo / archive / Python 插件等内置源（重名保护）
        返回本次注册成功的源数量。任何源失效只影响自身，不影响其他源。"""
        if self.online_manager is None:
            return 0
        from app.music.online.sources.http_source import HttpJsonSource
        from app.music.source_manager import MusicSourceManager

        # 1) 注销上次同步的自定义 HTTP 源
        for name in list(self._custom_http_sources):
            self.online_manager.unregister(name)
        self._custom_http_sources = set()

        # 2) 按当前配置重建
        cfg_mgr = MusicSourceManager()
        registered = 0
        for s in cfg_mgr.get_all_sources():
            if not s.enabled or not s.api_url:
                continue
            # source_type=js 的源可能是 UI 导入 .js 时提取的元信息（洛雪插件），
            # 但只要用户填写了 api_url 且已启用，仍按现有 HTTP JSON 协议尝试注册，
            # 保留旧配置兼容（如 ikun 音源）。若协议不符，搜索失败会记入 _errors
            # 且不影响其他音源。api_url 为空的源在上一行已被跳过。
            if s.source_type == "js":
                logger.info(f"[Music] 自定义源 '{s.name}' 为 js 元信息，尝试按 HTTP JSON 协议注册"
                      f"（若失效请在 UI 中改用 JSON 导入）")
            # 重名保护：name 已被内置/插件源占用时跳过，避免覆盖 js_bridge 等
            if s.name in self.online_manager._sources:
                logger.info(f"[Music] 自定义源 '{s.name}' 与已注册音源重名，跳过")
                continue
            src = HttpJsonSource(
                name=s.name,
                api_url=s.api_url,
                search_path=s.search_path,
                song_url_path=s.song_url_path,
            )
            self.online_manager.register(src)
            self._custom_http_sources.add(s.name)
            registered += 1
        if registered:
            logger.info(f"[Music] 自定义音源已同步: {sorted(self._custom_http_sources)}")
        return registered

    def search_online(self, keyword: str) -> dict:
        if self.online_manager is None:
            return {}
        return self.online_manager.search_all(keyword)

    def reload_online_plugins(self) -> tuple[bool, int]:
        """重载 JS 插件桥接（重新扫描插件目录）并清空其搜索缓存。

        返回 (ok, 插件数)。任何一步失败 → ok=False 且保留失败信息，不假装成功。
        清缓存是关键：桥接重载后若不失效 js_bridge 的搜索缓存，旧结果会继续
        掩盖新插件（缓存 TTL 内搜索不到新导入的插件）。
        """
        if self.online_manager is None:
            return False, 0
        js = self.online_manager.get_source("js_bridge")
        if js is None:
            return False, 0
        ok = False
        try:
            ok = bool(js.reload())
        except Exception as e:
            logger.info(f"[Music] 插件刷新异常: {e!r}")
        n = 0
        try:
            n = len(js.list_plugins() or [])
        except Exception:
            pass
        try:
            self.online_manager.invalidate("js_bridge")
        except Exception as e:
            logger.info(f"[Music] 插件缓存清理失败: {e!r}")
        return ok, n

    def get_online_sources(self) -> list:
        if self.online_manager is None:
            return []
        return self.online_manager.list_sources()

    def get_top_lists(self) -> dict:
        """各源榜单分组（源自报能力）：{源或插件名: [分组]}。失败 → {} 不抛。"""
        if self.online_manager is None:
            return {}
        try:
            return self.online_manager.get_top_lists()
        except Exception as e:
            logger.info(f"[Music] get_top_lists fail: {e}")
            return {}

    def get_top_list_detail(self, source: str, list_id: str, page: int = 1):
        """单榜单详情：{is_end, tracks: [OnlineTrack]}。失败 → None。"""
        if self.online_manager is None:
            return None
        try:
            return self.online_manager.get_top_list_detail(source, list_id, page)
        except Exception as e:
            logger.info(f"[Music] get_top_list_detail fail: {e}")
            return None

    def search_local(self, keyword: str, limit: int = 200) -> list:
        """搜索本地库，返回 [{id, path, title, artist, album}, ...]"""
        try:
            if not keyword:
                rows = self.db._conn.execute(
                    "SELECT id, path, title, artist, album FROM music LIMIT ?", (limit,)
                ).fetchall()
            else:
                kw = f"%{keyword}%"
                rows = self.db._conn.execute(
                    "SELECT id, path, title, artist, album FROM music "
                    "WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? LIMIT ?",
                    (kw, kw, kw, limit),
                ).fetchall()
            return [
                {"id": r[0], "path": r[1], "title": r[2] or "",
                 "artist": r[3] or "", "album": r[4] or ""}
                for r in rows
            ]
        except Exception as e:
            logger.error(f"[Music] search_local error: {e}")
            return []

    def play_online_resume(self, generation: int, idx: int) -> bool:
        """B 方案：候选换源重播（异步 + 按源分组 failover）。

        立即返回 True（已接受请求）；实际换源在后台线程执行：
        - 从点击候选的源开始，按列表首次出现顺序试其他源；
        - 每源最多 2 条候选，最多 5 个源；
        - 每条候选：取 URL -> loadfile -> 等 2s 检查是否真播起来；
        - 成功 -> 从 0:00 重播（不保进度）；全部失败 -> 发 ONLINE_ERROR；
        - 用户操作（_cancel_candidate_switch）可随时中断。
        """
        # 打点：意图入口（不持锁，lock_wait_ms 记为 0）
        self._record_intent("play_online_resume", self._current_song, 0,
                            extra={"generation": int(generation), "idx": int(idx)})
        try:
            if generation != self._candidate_generation:
                return False
            cands = self._active_candidates
            if not cands or idx < 0 or idx >= len(cands):
                return False


            self._candidate_switch_token += 1
            my_token = self._candidate_switch_token
            self._candidate_switching = True

            start_source = cands[idx].source_name
            gen_snapshot = self._candidate_generation

            def _try_switch():
                try:
                    src_order = []
                    seen = set()
                    for c in cands:
                        if c.source_name not in seen:
                            seen.add(c.source_name)
                            src_order.append(c.source_name)
                    if start_source in src_order:
                        src_order.remove(start_source)
                        src_order.insert(0, start_source)
                    src_order = src_order[:5]

                    import time as _t

                    for src in src_order:
                        if my_token != self._candidate_switch_token:
                            logger.info("[Music] 换源被用户操作取消")
                            return
                        src_cands = [c for c in cands if c.source_name == src][:2]
                        for c in src_cands:
                            if my_token != self._candidate_switch_token:
                                return
                            if gen_snapshot != self._candidate_generation:
                                return
                            ok = self.play_online(c, _before_load=lambda: my_token == self._candidate_switch_token)
                            if not ok:
                                continue
                            if my_token != self._candidate_switch_token:
                                return
                            played = False
                            for _ in range(4):
                                _t.sleep(0.5)
                                if my_token != self._candidate_switch_token:
                                    return
                                try:
                                    idle_now = bool(self.mpv.get_property("idle-active", timeout=0.5))
                                except Exception:
                                    idle_now = True
                                if not idle_now:
                                    played = True
                                    break
                            if played:
                                if my_token != self._candidate_switch_token:
                                    return
                                logger.info(f"[Music] 换源成功: {src}")
                                return
                    if my_token == self._candidate_switch_token:
                        logger.info("[Music] 该歌所有音源均无法播放")
                        try:
                            self.event_bus.publish(Event(
                                event_type=ONLINE_ERROR,
                                data={"message": "该歌所有音源均无法播放"},
                                source="music",
                            ))
                        except Exception:
                            pass
                finally:
                    if my_token == self._candidate_switch_token:
                        self._candidate_switching = False

            threading.Thread(target=_try_switch, daemon=True, name="music-resume-failover").start()
            return True
        except Exception as e:
            logger.error(f"[Music] play_online_resume error: {e}")
            return False

    def play_online_list(self, tracks: list, index: int = 0) -> bool:
        """在线歌单播放（酷狗式）：点击第 index 首立即播放，其后歌曲批量入队。

        队列内在线歌由 _play_song 的 online 分支重搜播放（title+artist 重查 URL）；
        播完整个歌单队列为空 → _next_locked 保持空闲，绝不跨界加载本地库。
        切歌单即替换队列（clear + add_all 剩余），符合"在当前来源内连续播放"语义。
        tracks 为 OnlineTrack 列表（panel 榜单缓存 self._rank_tracks）。"""
        if not self._mpv_available or self.online_manager is None:
            return False
        if not tracks or not 0 <= index < len(tracks):
            return False
        rest = []
        for t in tracks[index + 1:]:
            rest.append({
                "title": getattr(t, "title", "") or "",
                "artist": getattr(t, "artist", "") or "",
                "album": getattr(t, "album", "") or "",
                "duration": getattr(t, "duration_seconds", 0) or 0,
                "source_type": "online",
                "source_name": getattr(t, "source_name", "") or "",
                "track_id": getattr(t, "track_id", "") or "",
                "path": "",
                "filename": "",
                "music_id": 0,
                "id": 0,
                # 完整 extra 随队（含 _raw 原始歌曲对象）：_resolve_online_worker 直取
                # 播放依赖 raw_item 构造插件 URL（缺了会退化到重搜，命中片段版）。
                "extra": getattr(t, "extra", None) or {},
            })
        with self._lock:
            # 切换歌单 = 替换队列：清掉旧队列（本地/其它在线残留），只留本歌单剩余歌
            self.queue.clear()
            if rest:
                self.queue.add_all(rest)
        return self.play_online(tracks[index])

    def _end_switch_state_if_current(self, req_id: int) -> None:
        """请求失败收口：仅当本请求仍是最新播放意图时结束切换态。

        play_online 置 _is_switching_track=True 后、正式 loadfile 前的失败
        return 路径（mpv 不可用 / online_manager 缺失 / 无 URL）统一调用。
        锁内代次校验：失败窗口内若已有新播放意图（代次递增）接管，旧请求
        不得清除新请求的切换态；代次未变则结束本请求遗留的切换态。
        """
        with self._lock:
            if self._play_request_id == req_id:
                self._is_switching_track = False

    def play_online(self, track, _before_load=None) -> bool:
        """通过 online source 获取 URL 后用 mpv 直接播放。"""
        # 打点：意图入口（不持锁，lock_wait_ms 记为 0）
        self._record_intent("play_online", track, 0)
        # 代次隔离：play_online 是不经 _play_song 的公开入口，同样递增代次
        # 并置切换态——URL 解析（get_play_url）窗口内 mpv 仍报旧歌位置，必须失效。
        # 递增 + 快照 + 切换态在锁内完成（与 _play_song/play_search_result 同契约），
        # 避免无锁 read-modify-write 丢代次导致旧请求误判有效。
        with self._lock:
            self._play_request_id += 1
            req_id = self._play_request_id
            self._is_switching_track = True
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            self._end_switch_state_if_current(req_id)
            return False
        if self.online_manager is None:
            self._end_switch_state_if_current(req_id)
            return False
        result = self.online_manager.get_play_url(track)
        if not result:
            logger.info(f"[Music] online 无 URL: {track.title}")
            self._end_switch_state_if_current(req_id)
            return False
        url = result.get("url")
        logger.info(f"[Music] play online: {track.title} -> {url[:80]}")
        try:
            # 锁内权威校验 + 播放（与 _resolve_online_worker 同契约）：
            # get_play_url 是慢网络调用（无锁窗口），返回后必须锁内核对代次——
            # 期间 play_song/next 等持锁入口可能已发起新意图，校验失败绝不 loadfile。
            # 校验与 loadfile 之间无新请求可插入（本锁与所有播放入口互斥）。
            with self._lock:
                if req_id != self._play_request_id:
                    logger.info("[Music] play_online 已过期（期间已切歌），丢弃结果")
                    return False
                # 统一流设置（DRY）：header 重设 + before_load 紧贴 loadfile（保持
                # "校验→loadfile"原子）+ loadfile + pause + 状态落库。
                online_song = self._apply_stream(
                    track, url, result.get("headers"), before_load=_before_load)
                if online_song is None:
                    # 互斥确认或紧邻 loadfile 前置校验失败时，没有异常可供
                    # 外层 except 收口；必须结束本次切换态，避免 UI 永久冻结。
                    # 此处已经持有 _lock，直接完成等价的代次守卫，避免
                    # 再次获取非可重入锁造成失败路径死锁。
                    if req_id == self._play_request_id:
                        self._is_switching_track = False
                    return False
                logger.info("[Music] mpv loadfile ok")
        except Exception as e:
            logger.info(f"[Music] mpv loadfile fail: {e}")
            return False
        self.remember_cover(
            title=track.title or "",
            artist=track.artist or "",
            source_name=getattr(track, "source_name", "") or "",
            track_id=getattr(track, "track_id", "") or "",
            cover_url=getattr(track, "cover_url", None) or "")
        # _current_song/_update_state 已由 _apply_stream 统一更新（id=-1）
        # 记录播放历史（直接调用 record_song，不走 MUSIC_TRACK_CHANGED——
        # 该事件另有 rule_engine/speech_judge 语音订阅者，不能因播放而误触）
        try:
            self.history_recorder.record_song({
                "id": -1,
                "title": track.title,
                "artist": track.artist,
                "source": "online",
                "source_name": getattr(track, "source_name", "") or "",
                "track_id": getattr(track, "track_id", "") or "",
            })
        except Exception as e:
            logger.error(f"[Music] 记录播放历史失败: {e}")
        return True

    def set_volume(self, volume: int):
        """设置音量"""
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return False
        volume = max(0, min(100, volume))
        self.mpv.set_volume(volume)
        self.state_store.set(StateNamespace.MUSIC, "volume", volume)

    def volume_up(self, step: int = 10):
        """音量增加"""
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return False
        current = self.mpv.get_volume()
        return self.set_volume(current + step)

    def volume_down(self, step: int = 10):
        """音量减少"""
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return False
        current = self.mpv.get_volume()
        return self.set_volume(current - step)

    def get_volume(self) -> int:
        """获取当前音量"""
        return self.mpv.get_volume()

    # ===== 倍速 =====
    # 允许档位（与电影侧 movie_panel._speed_steps 一致）
    SPEED_STEPS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)

    def set_speed(self, rate: float) -> bool | None:
        """设置播放倍速。

        复用 mpv 全局 speed 属性（切歌保留，mpv 进程自愈重启后会重置为 1.0）。
        True 为 MPV 协议确认成功；False 为拒绝/协议失败；None 为结果未知。
        仅明确成功才记录新档位，IPC 异常原样向上抛出，不重试。
        """
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            logger.warning(f"[Music] 非法倍速: {rate!r}")
            return False
        # 档位精确匹配（浮点容差 0.001）
        if not any(abs(rate - s) < 0.001 for s in self.SPEED_STEPS):
            logger.warning(f"[Music] 倍速 {rate} 不在允许档位 {self.SPEED_STEPS}")
            return False
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略倍速")
            return False
        response = self.mpv.set_property("speed", rate)
        if not isinstance(response, dict) or not isinstance(response.get("error"), str):
            logger.warning("[Music] 倍速设置结果未确认，保留上次确认的档位")
            return None
        if response["error"] != "success":
            logger.warning("[Music] 倍速设置失败（MPV 协议错误），保留原档位")
            return False
        self._speed = rate
        try:
            self.state_store.set(StateNamespace.MUSIC, "speed", rate)
        except Exception:
            pass
        logger.info(f"[Music] 倍速 -> {rate}x")
        return True

    def get_speed(self) -> float:
        """当前倍速（Controller 记录值；mpv 未启动时为上次设定/默认 1.0）。"""
        return self._speed

    def set_sleep_timer(self, seconds) -> bool:
        """设置单一音乐睡眠定时器；到期停止当前音乐，不自动恢复。"""
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return False
        if seconds <= 0 or seconds > 24 * 60 * 60:
            return False
        with self._sleep_timer_lock:
            old = self._sleep_timer
            if old is not None:
                old.cancel()
            timer = threading.Timer(seconds, self._sleep_timer_expired)
            timer.daemon = True
            self._sleep_timer = timer
            timer.start()
        self.event_bus.publish(Event(
            event_type=EventType.MUSIC_STATE_CHANGED,
            data={"state": "sleep_timer", "active": True, "seconds": seconds},
            source="music",
        ))
        return True

    def cancel_sleep_timer(self) -> bool:
        with self._sleep_timer_lock:
            timer = self._sleep_timer
            self._sleep_timer = None
            if timer is None:
                return False
            timer.cancel()
        self.event_bus.publish(Event(
            event_type=EventType.MUSIC_STATE_CHANGED,
            data={"state": "sleep_timer", "active": False, "seconds": 0},
            source="music",
        ))
        return True

    def _sleep_timer_expired(self):
        with self._sleep_timer_lock:
            self._sleep_timer = None
        self.event_bus.publish(Event(
            event_type=EventType.MUSIC_STATE_CHANGED,
            data={"state": "sleep_timer", "active": False, "seconds": 0},
            source="music",
        ))
        if not self._running:
            return
        try:
            self.stop_current()
        except Exception as e:
            logger.error(f"[Music] 睡眠定时停止失败: {e}")

    # ===== 歌单/榜单语音命令 =====
    def set_voice_rank_tracks(self, tracks: list | None):
        """UI 榜单加载成功后调用，供语音播放榜单第N首。"""
        self._voice_rank_tracks = tracks or []

    def set_movie_pauser(self, fn):
        """注入电影暂停确认回调：fn() -> bool（True=确认已暂停）。"""
        self._movie_pauser = fn

    def _cmd_play_playlist(self, command: Command) -> bool:
        name = command.params.get("name", "")
        if not name:
            return False
        playlists = self.get_playlists()
        matches = [p for p in playlists if p.get("name") == name]
        if not matches:
            return False
        if len(matches) > 1:
            return False  # 重名不静默选择
        songs = self.get_playlist_songs(matches[0]["id"])
        if not songs:
            return False
        # 播第一首 + 其余入队（逆序插入 add_next 保持原顺序）
        ok = self.play_playlist_item(songs[0])
        if not ok:
            return False
        for s in reversed(songs[1:]):
            try:
                if not self.add_next(s):
                    logger.error(f"[Music] 歌单入队失败: {s.get('title')}")
                    return False
            except Exception as e:
                logger.error(f"[Music] 歌单入队异常: {e}")
                return False
        return True

    def _cmd_create_playlist(self, command: Command) -> bool:
        name = command.params.get("name", "").strip()
        if not name:
            return False
        return self.create_playlist(name) is not None

    def _cmd_add_to_playlist(self, command: Command) -> bool:
        name = command.params.get("name", "")
        if not name:
            return False
        playlists = self.get_playlists()
        matches = [p for p in playlists if p.get("name") == name]
        if len(matches) != 1:
            return False
        song_query = command.params.get("song_query", "")
        if song_query:
            # 指定歌曲：本地搜索
            try:
                local = self.search.search(song_query, limit=5)
            except Exception:
                local = []
            for s in local:
                if s.get("path") and os.path.exists(s["path"]):
                    # 重复添加视为幂等成功
                    try:
                        added = self.add_to_playlist(matches[0]["id"], s)
                        if added is True:
                            return True
                        # False can mean duplicate OR a caught storage error.
                        # Idempotent success needs evidence the song exists.
                        music_id = int(s.get("music_id") or s.get("id") or 0)
                        return music_id > 0 and any(
                            row.get("source_type") == "local" and row.get("music_id") == music_id
                            for row in self.get_playlist_songs(matches[0]["id"]))
                    except Exception:
                        return False
            return False
        song = self.get_current_song()
        if not song:
            return False
        return self.add_to_playlist(matches[0]["id"], song)

    def _cmd_add_next(self, command: Command) -> bool:
        query = command.params.get("query", "")
        if not query:
            return False
        # 1. 本地优先
        try:
            local = self.search.search(query, limit=5)
        except Exception as e:
            logger.error(f"[Music] add_next local search error: {e}")
            local = []
        for s in local:
            path = s.get("path") or ""
            if path and os.path.exists(path):
                return self.add_next(s)
        # 2. 在线兜底
        if self.online_manager is None:
            return False
        try:
            results = self.online_manager.search_all(query)
        except Exception as e:
            logger.error(f"[Music] add_next online search error: {e}")
            return False
        if not results:
            return False
        tracks = [track for group in results.values() if isinstance(group, list) for track in group] if isinstance(results, dict) else []
        if not tracks:
            return False
        track = tracks[0]
        song = {
            "title": getattr(track, "title", "") or "",
            "artist": getattr(track, "artist", "") or "",
            "source_type": "online",
            "source_name": getattr(track, "source_name", "") or "",
            "track_id": getattr(track, "track_id", "") or "",
            "extra": getattr(track, "extra", None) or {},
        }
        return self.add_next(song)

    def _cmd_play_rank_index(self, command: Command) -> bool:
        idx = command.params.get("index", -1)
        tracks = getattr(self, "_voice_rank_tracks", None)
        if not tracks or not isinstance(idx, int) or not 0 <= idx < len(tracks):
            return False
        return self.play_online_list(tracks, idx)

    # ===== 跳转 =====

    @_cancel_switch_on_user_action
    def seek(self, seconds: float, mode: str = "relative"):
        """跳转"""
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return
        # seek 防抖：发送前置位，mpv seek 会短暂进入 idle（缓冲窗口），
        # 该窗口内 _poll_idle 不得误判为播放结束触发 _auto_next；
        # 1.5s 后由 daemon Timer 复位（保底覆盖 seek 缓冲/网络抖动）。
        self._is_seeking = True
        timer = threading.Timer(1.5, lambda: setattr(self, "_is_seeking", False))
        timer.daemon = True
        timer.start()
        self.mpv.seek(seconds, mode)
        # seek 成功后同步快照目标值（绝对 → 目标；相对 → 当前外推位置 + delta），
        # 避免切歌/seek 后进度跳回旧缓存。
        try:
            if mode == "absolute":
                target = max(0.0, float(seconds))
            else:
                target = max(0.0, self.current_playback_time + float(seconds))
            self._playback_time_cache = (target, time.perf_counter(), self._paused_snapshot)
        except Exception:
            pass

    @_cancel_switch_on_user_action
    def jump_to_highlight(self):
        """跳到高潮部分（一次性）"""
        if not self._mpv_available:
            logger.info("[Music] mpv 不可用，忽略操作")
            return
        if not self._current_song:
            return

        try:
            # mpv 异常/未连接时可能返回 None 或非数字，统一收敛为 0（视为时长未知）
            raw_duration = self.mpv.get_duration()
            duration = float(raw_duration) if isinstance(raw_duration, (int, float)) else 0.0
        except Exception:
            duration = 0.0

        song_id = self._current_song.get("id")
        highlight = self.db.get_highlight(song_id)
        if highlight:
            target = float(highlight.get("start_time") or 0)
            # 边界保护：不超歌曲时长、不为负（highlight 数据可能来自过期文件）
            if duration > 0:
                target = min(max(0.0, target), max(0.0, duration - 1.0))
            try:
                self.mpv.seek(target, "absolute")
            except Exception as e:
                logger.error(f"[Music] highlight seek 失败: {e}")
                return
            self.event_bus.publish(Event(
                event_type=EventType.MUSIC_HIGHLIGHT,
                data={"song_id": song_id, "position": target},
                source="music"
            ))
        else:
            # 没有高潮数据，跳到 60% 位置作为兜底
            if duration > 0:
                try:
                    self.mpv.seek(duration * 0.6, "absolute")
                except Exception as e:
                    logger.error(f"[Music] highlight fallback seek 失败: {e}")

    # ===== Highlight Mode =====

    @_cancel_switch_on_user_action
    def set_highlight_mode(self, enabled: bool):
        """开启或关闭高潮模式"""
        self._highlight_mode = enabled
        if not enabled:
            self._invalidate_highlight()
        else:
            # 如果当前有歌曲，立即开始 highlight 段
            if self._current_song:
                self._invalidate_highlight()
                self._start_highlight_segment(self._current_song)
        self.state_store.set(StateNamespace.MUSIC, "highlight_mode", enabled)

    def is_highlight_mode(self) -> bool:
        return self._highlight_mode

    def mark_highlight_start(self) -> bool:
        """用户手动标记当前播放位置为高潮起点（+35 秒段）。

        同一首歌重复标记时，先清除旧的 manual 记录，避免多行 manual 导致
        get_highlight 的 ORDER BY confidence DESC 结果不确定。
        """
        song = self.get_current_song()
        if not song:
            return False
        song_id = song["id"]
        pos = self.mpv.get_live_playback_time()
        if not pos or pos < 5:
            return False
        end = min(pos + 35.0, self.mpv.get_duration() or pos + 35.0)
        # 只删 manual 记录，不动其他来源（pychorus/v4 等自动结果由 scanner 维护）
        self.db.clear_manual_highlight(song_id)
        self.db.save_highlight(song_id, pos, end, confidence=1.0, source="manual")
        logger.info(f"[Music] manual highlight {song.get('title')}: {pos:.1f}-{end:.1f}s")
        return True

    def get_play_mode(self) -> str:
        return self.queue.get_play_mode()

    def _start_highlight_segment(self, song: dict[str, Any]):
        """为当前歌曲启动 highlight 播放段。

        有预计算 highlight → seek 到 start_time 并启动 end 轮询自动 next；
        无 highlight → 与单次"播放高潮"一致，seek 到时长 60% 位置兜底，
        不启动 end 轮询（不知道 end_time，让用户听完剩余部分即可）。
        """
        song_id = song.get("id")
        highlight = self.db.get_highlight(song_id)
        if not highlight:
            # 无预计算高潮时间：seek 到 60% 位置作为兜底（与 jump_to_highlight 一致）。
            # 旧逻辑静默 return → 模式开着但整首从头播，用户误以为开关失效。
            try:
                dur = float(self.mpv.get_duration() or 0.0)
            except Exception:
                dur = 0.0
            if dur > 0:
                target = dur * 0.6
                gen = self._highlight_gen
                def _fallback_seek(gen_check, t):
                    import time
                    time.sleep(0.6)
                    if gen_check != self._highlight_gen:
                        return
                    try:
                        self.mpv.seek(t, "absolute")
                        logger.info(f"[Music] 高潮模式(无预计算): seek 到 60% = {t:.1f}s")
                    except Exception as e:
                        logger.info(f"[Music] highlight fallback seek failed: {e}")
                threading.Thread(target=_fallback_seek, args=(gen, target), daemon=True).start()
            return

        start = highlight["start_time"]
        end = highlight["end_time"]
        if end is None or end <= start:
            return

        # 延迟 seek（等 mpv 加载新曲目）
        def _delayed_seek(gen_check, start_sec):
            import time
            time.sleep(0.6)
            if gen_check != self._highlight_gen:
                return
            try:
                self.mpv.seek(start_sec, "absolute")
                logger.info(f"[Music] highlight seek to {start_sec:.1f}s")
            except Exception as e:
                logger.info(f"[Music] highlight seek failed: {e}")

        # 生成新 generation，使旧 poller 失效
        self._highlight_gen += 1
        gen = self._highlight_gen
        self._highlight_end_time = end
        self._highlight_song_id = song_id
        self._highlight_stop = threading.Event()

        self.event_bus.publish(Event(
            event_type=EventType.MUSIC_HIGHLIGHT,
            data={"song_id": song_id, "position": start, "mode": "segment"},
            source="music"
        ))

        # 延迟 seek 线程
        threading.Thread(target=_delayed_seek, args=(gen, start), daemon=True).start()

        # 启动 poller
        self._highlight_poller = threading.Thread(
            target=self._highlight_poll_loop,
            args=(gen, song_id, end),
            daemon=True,
            name="highlight-poller"
        )
        self._highlight_poller.start()

    def _invalidate_highlight(self):
        """使当前 highlight poller 失效"""
        self._highlight_stop.set()
        self._highlight_gen += 1
        self._highlight_end_time = None
        self._highlight_song_id = None

    # ===== 播报 ducking =====

    def _on_speech_started(self, event: Event):
        """TTS 播报开始发声 → 音乐音量降到原值的 duck_volume_pct%。
        仅在音乐真实播放时 duck；已 duck 或 mpv 不可用则跳过。"""
        if not self._mpv_available:
            return
        if self._duck_active:
            return
        try:
            idle_now = bool(self.mpv.get_property("idle-active", timeout=0.5))
            paused_now = bool(self.mpv.get_property("pause", timeout=0.5))
            if idle_now or paused_now:
                return
        except Exception:
            return
        try:
            orig = int(self.mpv.get_volume())
        except Exception:
            return
        self._duck_original_volume = orig
        ducked = max(0, min(100, int(orig * self.duck_volume_pct / 100)))
        self.set_volume(ducked)
        self._duck_active = True

    def _on_speech_finished(self, event: Event):
        """TTS 播报结束（completed/stopped 统一处理）→ 恢复音乐音量。
        用户 duck 期间手动调过音量 → 不覆盖用户意图。"""
        if not self._duck_active:
            return
        self._duck_active = False
        orig = self._duck_original_volume
        self._duck_original_volume = None
        if orig is None:
            return
        ducked = max(0, min(100, int(orig * self.duck_volume_pct / 100)))
        try:
            current = int(self.mpv.get_volume())
        except Exception:
            current = None
        if current is not None and current == ducked:
            self.set_volume(orig)

    def _highlight_poll_loop(self, gen: int, song_id: int, end_time: float):
        """Highlight 段轮询线程：到达 end_time 自动 next"""
        stop = self._highlight_stop
        while not stop.wait(timeout=0.4):
            # 校验 generation 和 song_id
            if gen != self._highlight_gen:
                return
            if song_id != self._highlight_song_id:
                return
            if not self._highlight_mode:
                return
            if not self._running:
                return

            try:
                pos = self.mpv.get_live_playback_time()
            except Exception:
                continue

            if pos >= end_time:
                # 再次校验，防止竞态
                if gen == self._highlight_gen and song_id == self._highlight_song_id:
                    self._invalidate_highlight()
                    self.next()
                return

    # ===== 扫描 =====

    def rescan(self, full: bool = False) -> dict[str, int]:
        """重新扫描音乐库"""
        logger.info(f"[Music] 开始扫描音乐库 (full={full})...")
        result = self.scanner.scan(full_rescan=full)
        logger.info(f"[Music] 扫描完成: {result}")
        self.state_store.set(StateNamespace.MUSIC, "song_count", self.db.get_song_count())
        # 扫描完成后后台计算highlight，不阻塞
        self.scanner.start_highlight_background()
        return result

    def cleanup_missing(self) -> dict[str, int]:
        """清理文件已不存在的歌曲记录（含其 highlight）"""
        result = self.db.delete_missing_songs()
        logger.info(f"[Music] 清理失效歌曲: {result}")
        self.state_store.set(StateNamespace.MUSIC, "song_count", self.db.get_song_count())
        return result

    def get_song_count(self) -> int:
        """获取歌曲总数"""
        return self.db.get_song_count()

    # ===== 队列管理 =====

    def get_queue(self) -> list[dict[str, Any]]:
        """获取播放队列"""
        return self.queue.get_queue()

    @_cancel_switch_on_user_action
    def clear_queue(self):
        """清空队列：同时停止当前播放、失效 highlight、重置轮询基线。
        旧行为只清队列数据 → 播完自动重载全库续播（清空无意义 + 状态错乱）。"""
        # 打点：意图入口（实际记录在锁内）
        _t_enter = time.perf_counter() if (self._trace and self._trace.enabled) else None
        with self._lock:
            # 意图打点：已获取锁（记录清空前的当前曲目）
            if _t_enter is not None:
                self._record_intent(
                    "clear_queue", self._current_song,
                    round((time.perf_counter() - _t_enter) * 1000, 2))
            self._invalidate_highlight()
            self._current_song = None
            self.queue.clear()
            try:
                self.mpv.stop()  # mpv 回 idle，停止当前播放
            except Exception:
                pass
            # 关键：与 mpv 实际状态（idle）对齐，防止下一轮轮询把"停止后 idle"
            # 误判为"播完上升沿"→ 自动切歌 → 队列空 → 全库重载续播
            self._last_idle = True
            self._update_state(playing=False, current_song=None)

    def stop_current(self):
        """停止当前歌曲播放（保留队列，不推进队列指针）。

        与 clear_queue() 的差别：不清空队列、不改动 queue.current_index——
        用户主动"播放/继续"时 play() 的 idle 重载路径重播当前曲目，
        用户主动"下一首"时才推进。
        防自动续播：与 clear_queue 同款 _last_idle=True 防护，但必须先置位
        再 mpv.stop()——mpv 进入 idle 的时刻必然晚于基线置位，轮询读到的
        now_idle 与 _last_idle 同为 True，不构成上升沿，不会误触发 _auto_next；
        _next_locked 锁内另有 origin=auto + _last_idle 双重守卫兜底竞争窗口。

        三态处理（与 mpv.stop() 返回值对应）：
        明确成功（True，协议层 error=="success"）→ 清 _current_song、置
        _last_idle=True、playing=False；
        明确失败（False，MPV 错误响应）或调用抛异常 → 记录错误日志，保留
        停止前的 _current_song/_last_idle/playing，不宣称停止成功；
        结果未确认（None，IPC 超时 / 管道失效 / 无响应，命令可能已执行）→ 记录
        错误日志，保留停止前状态；
        不自动重试——超时后命令可能已经执行，盲目重试只会增加不确定性。
        成功确认仅为 MPV 协议层确认，不代表扬声器已物理静音。
        """
        with self._lock:
            self._invalidate_highlight()
            self._user_stopped = True  # V2-4.5.6：标记用户主动停止，阻止 idle/EOF 自动续播
            try:
                result = self.mpv.stop()  # True=成功 / False=错误响应 / None=未确认
            except Exception as e:
                logger.error(
                    f"[Music] stop_current: mpv.stop() 失败，停止状态未确认: {e!r}")
                return
            if result is True:
                self._current_song = None
                self._last_idle = True
                self._update_state(playing=False, current_song=None)
            elif result is False:
                logger.error(
                    "[Music] stop_current: mpv.stop() 返回错误响应，停止未执行，保留停止前状态")
                return False
            else:  # None：IPC 超时/管道失效/无响应 → 结果未确认
                logger.error(
                    "[Music] stop_current: mpv.stop() 结果未确认（IPC 超时/管道失效/无响应，命令可能已执行），保留停止前状态")
                # V2-4.5.5：即便结果未确认，mpv.stop() 大概率已执行，mpv 即将进入 idle。
                # 必须置 _last_idle=True，防止轮询把"停止导致的 idle"误判为自然播完而自动续播。
                self._last_idle = True
                return
            return True

    def set_play_mode(self, mode: str):
        """设置播放模式（顺序/随机/单曲/列表循环）+ 写回 settings.yaml music.play_mode。
        延迟 import：app/music 不依赖 app/ui 的模块级导入（防环）。"""
        self.queue.set_play_mode(mode)
        try:
            from app.ui.settings_store import save_settings_yaml
            save_settings_yaml({"music": {"play_mode": mode}})
        except Exception as e:
            logger.error(f"[Music] 播放模式写回配置失败: {e!r}")

    def get_current_song(self) -> dict[str, Any] | None:
        """获取当前播放歌曲"""
        return self._current_song

    def get_current_time(self) -> float:
        """获取当前播放位置"""
        return self.mpv.get_current_time()

    def get_duration(self) -> float:
        """获取当前歌曲总时长"""
        return self.mpv.get_duration()

    # ===== 命令处理器 =====

    def _cmd_volume_set(self, command: Command):
        volume = command.get("volume", 50)
        return self.set_volume(int(volume))

    def _cmd_seek(self, command: Command):
        seconds = command.get("seconds", 0)
        mode = command.get("mode", "relative")
        self.seek(float(seconds), mode)

    def _cmd_search_play(self, command: Command):
        query = command.get("query", "")
        index = command.get("index", 0)
        return self.play_search_result(query, int(index))

    def _cmd_highlight_mode(self, command: Command):
        enabled = command.get("enabled", True)
        self.set_highlight_mode(bool(enabled))

    def _cmd_set_play_mode(self, command: Command):
        mode = command.get("mode", "sequential")
        self.set_play_mode(mode)

    # ===== 事件处理 =====

    def _on_idle_change(self, value: Any):
        """mpv 空闲状态变化"""
        if value and self._running and not self._user_stopped:
            # 播放结束，自动下一首
            # 延迟一点，确保 eof 事件处理完
            threading.Timer(0.5, self._auto_next).start()

    def _on_eof(self, value: Any):
        """播放结束"""
        if value:
            self.event_bus.publish(Event(
                event_type=EventType.MUSIC_STATE_CHANGED,
                data={"state": "eof"},
                source="music"
            ))

    def _auto_next(self):
        """自动播放下一首"""
        # origin 上下文：自动切歌全程标记为 auto（_play_song 内部据此区分用户/自动意图）
        with self._origin_ctx("auto"):
            if self._candidate_switching:
                logger.info("[Music] 换源中，跳过自动切歌")
                return
            if not self._running:
                return
            if self._user_stopped:
                logger.info("[Music] auto_next: 用户已主动停止，跳过自动切歌")
                return
            # 守卫必须与 _poll_idle 同源（实时 get_property）。
            # 不能用 self.mpv.is_idle()：它读的是启动时初始化的属性缓存，
            # mpv 事件回调在 _send_command 中被丢弃，idle-active 缓存从不更新，
            # 初始读失败时默认 False → 会拦截已被实时确认的切歌（next() 永不执行）。
            try:
                idle_now = bool(self.mpv.get_property("idle-active", timeout=0.5))
            except Exception as e:
                logger.error(f"[Music] auto_next idle check error: {e!r}")
                return
            if not idle_now:
                logger.info("[Music] auto_next: idle-active 实时为 False，跳过切歌")
                return
            # 决策点：auto_next 真正触发（已确认 idle）
            self._record_decision({
                "kind": "auto_next_fired",
                # getattr 防御：_last_idle 在 start() 才初始化，打点绝不因此抛异常
                "last_idle": getattr(self, "_last_idle", None),
                "now_idle": idle_now,
                "queue_pos": self.queue.current_index
                    if hasattr(self.queue, "current_index") else -1,
            })
            logger.info("[Music] auto_next: 已确认 idle，执行 next()")
            try:
                self.next()
                logger.info("[Music] auto_next: next() 完成")
            except Exception as e:
                logger.info(f"[Music] auto_next: next() 异常: {e!r}")

    # ===== 影子对拍打点基建（意图/决策记录，完全透明：先执行原逻辑，记录异常吞掉） =====

    def _current_origin(self) -> str:
        """读取当前线程的 origin（缺省 'user'）。"""
        try:
            return getattr(self._tl, "origin", "user")
        except Exception:
            return "user"

    @contextlib.contextmanager
    def _origin_ctx(self, origin: str):
        """上下文管理器：进入后本线程 origin 切到指定值，退出恢复原值。"""
        old = getattr(self._tl, "origin", "user")
        self._tl.origin = origin
        try:
            yield
        finally:
            self._tl.origin = old

    def _record_intent(self, method: str, track, lock_wait_ms: float,
                       extra: dict | None = None) -> None:
        """意图打点（kind='intent'）。守卫未启用时零开销；记录异常绝不外抛。"""
        if not (self._trace and self._trace.enabled):
            return
        try:
            track_ref = self._track_ref(track)
            data = {
                "method": method,
                "origin": self._current_origin(),
                "track_ref": track_ref,
                "old_request_id": self._play_request_id,
                "lock_wait_ms": round(float(lock_wait_ms), 2),
            }
            if extra:
                data.update(extra)
            self._trace.record("intent", data)
            # 影子模式：同一意图并行喂给 reducer（只算 effects，绝不执行真实副作用）
            ev = self._shadow_event_for_intent(method, track_ref)
            if ev is not None:
                self._dispatch_shadow(ev)
        except Exception:
            pass

    def _record_decision(self, data: dict) -> None:
        """旧决策点打点（外层 kind='decision'）。守卫未启用时零开销；记录异常绝不外抛。

        注意：内层子类名（调用方习惯写 "kind": "auto_next_fired"）会被改写为
        "decision" 键，避免覆盖 recorder 的外层分类 kind（intent/decision/mpv）。"""
        if not (self._trace and self._trace.enabled):
            return
        try:
            d = dict(data)
            if "kind" in d:
                d["decision"] = d.pop("kind")
            self._trace.record("decision", d)
        except Exception:
            pass

    # 意图方法 → 影子状态机事件类型（与 scripts/replay_trace.py 的映射保持一致；
    # toggle / play_online_resume 语义依赖运行时状态，影子不映射，避免错误建模）
    _SHADOW_INTENT_MAP = {
        "play_song": PlaybackEventType.PLAY_REQUESTED,
        "play_online": PlaybackEventType.PLAY_REQUESTED,
        "play_search_result": PlaybackEventType.PLAY_REQUESTED,
        "next": PlaybackEventType.PLAY_REQUESTED,
        "prev": PlaybackEventType.PLAY_REQUESTED,
        "pause": PlaybackEventType.PAUSE_REQUESTED,
        "resume": PlaybackEventType.RESUME_REQUESTED,
        "play": PlaybackEventType.RESUME_REQUESTED,
        "clear_queue": PlaybackEventType.STOP_REQUESTED,
    }

    def _shadow_event_for_intent(self, method: str,
                                 track_ref: dict | None) -> PlaybackEvent | None:
        """intent 打点 → 影子状态机事件。epoch 由 reducer 自增，事件侧不携带。"""
        etype = self._SHADOW_INTENT_MAP.get(method)
        if etype is None:
            return None
        return PlaybackEvent(
            type=etype,
            track=track_ref,
            origin=self._current_origin(),
        )

    def _dispatch_shadow(self, event: PlaybackEvent) -> None:
        """影子模式执行器：把意图/采样事件喂给 PlaybackState reducer，
        只把计算出的 effects 记录到 trace（decision 子类 shadow_effects），
        绝不执行任何真实副作用（不碰 mpv/队列/状态库）。trace 关闭时零开销。"""
        if not (self._trace and self._trace.enabled):
            return
        try:
            with self._shadow_lock:
                old = self._shadow_state
                new_state, effects = reduce(old, event)
                self._shadow_state = new_state
                if effects:
                    # old_status 取 reduce 前状态（语义即"变更前"）
                    self._trace.record("decision", {
                        "decision": "shadow_effects",
                        "event": event.type.value,
                        "old_status": (old.status.name
                                       if old.status is not None else None),
                        "new_status": (new_state.status.name
                                       if new_state.status is not None else None),
                        "epoch": new_state.epoch,
                        "effects": [e.type.name for e in effects],
                    })
        except Exception:
            pass

    @staticmethod
    def _track_ref(track) -> dict:
        """曲目脱敏引用：本地歌只记文件名，在线歌只记 title 前 20 字符。"""
        if not track:
            return {"kind": "none"}
        if isinstance(track, dict):
            if track.get("path") and not str(track["path"]).startswith("http"):
                return {"kind": "local", "name": os.path.basename(str(track["path"]))}
            return {"kind": "online", "title": str(track.get("title", ""))[:20]}
        return {"kind": "unknown"}

    @staticmethod
    def _load_trace_enabled() -> bool:
        """影子对拍开关：读 config/settings.yaml 的 music.trace_enabled（默认 False）。
        读失败静默降级 False，绝不阻断启动。"""
        try:
            import os as _os
            import yaml
            from app.core.path_resolve import PROJECT_ROOT
            p = _os.path.join(PROJECT_ROOT, "config", "settings.yaml")
            if _os.path.exists(p):
                with open(p, "r", encoding="utf-8") as _f:
                    data = yaml.safe_load(_f) or {}
                return bool((data.get("music") or {}).get("trace_enabled", False))
        except Exception:
            pass
        return False

    def _update_state(self, **kwargs):
        """更新状态存储"""
        for key, value in kwargs.items():
            self.state_store.set(StateNamespace.MUSIC, key, value)

    @property
    def current_playback_time(self) -> float:
        """当前播放位置（秒）——读内存快照并外推，禁调 IPC。

        快照为 (pos, perf_counter, paused)，由后台线程更新（_poll_idle /
        _confirm_playback）或 seek/_play_song 同步写入。读取时按 (now - ts) 外推，
        消除 0.8s 采样 × 200ms 读取的阶梯感；暂停时不外推（位置冻结）。
        GUI 高频刷新（歌词 200ms、进度条）必须走本属性，绝不直接调 mpv。"""
        try:
            pos, ts, paused = self._playback_time_cache
            if paused:
                return float(pos)
            return float(pos + (time.perf_counter() - ts))
        except Exception:
            return 0.0

    def is_playing(self) -> bool:
        """是否正在播放"""
        if not self._mpv_available:
            return False
        return not self.mpv.is_paused() and not self.mpv.is_idle()








