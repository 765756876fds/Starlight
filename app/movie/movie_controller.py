"""
电影链控制器 - 苹果 CMS 搜索 + mpv 独立视频实例播放。

- 与音乐链互斥：确认电影实际起播后暂停音乐；音乐开始播新歌时
  电影订阅 MUSIC_TRACK_CHANGED 事件自行暂停（事件解耦）。
- mpv 独立实例（video=True 出视频窗口，taskkill=False 不杀音乐实例）。
- 剧集选集 + 自动连播（idle 上升沿检测）+ Assrt 字幕（搜索/下载/解压/加载）。
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from collections import Counter
from difflib import SequenceMatcher
from functools import wraps

import requests
from urllib3.exceptions import ReadTimeoutError

from ..core.config import get_config, resolve_mpv_path
from ..core.event_bus import EventType, get_event_bus
from ..core.logger import get_logger
from ..core.path_resolve import PROJECT_ROOT
from ..core.state_store import StateNamespace, get_state_store
from ..music.mpv import MpvController
from .cms_parser import (
    episode_eps,
    normalise_playback,
    first_play_url as first_play_url,
    parse_cms_response as parse_cms_response,
    split_play_url as split_play_url,
)
from .source_manager import MovieSourceManager, _SingleSourceManager
from .history import MovieHistory

logger = get_logger()

# 默认源：360资源（苹果 CMS 标准接口）
DEFAULT_SOURCE_URL = "https://360zyzz.com/api.php/provide/vod/from/360m3u8/at/json"
DEFAULT_IPC_PIPE = r"\\.\pipe\lol_movie_mpv"
# Assrt 字幕 API（直连被重置，必须走代理——已实测）
ASSRT_BASE = "https://api.assrt.net/v1/sub"
PROXY = "http://127.0.0.1:7897"  # 兼容默认；可被 movie.assrt_proxy 配置覆盖（空串=直连）
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 字幕假条目（网盘分享链接说明）：内容含这些 → 跳过
_FAKE_SUB_MARKERS = (b"pan.baidu.com", b"\xe7\x99\xbe\xe5\xba\xa6\xe7\xbd\x91\xe7\x9b\x98")
# 真字幕内容标记
_REAL_SUB_MARKERS = (b"-->", b"[Script Info]")


def _mpv_buffer_args(config) -> list:
    """电影 mpv 网络缓冲参数（卡顿优化，包2d）。
    settings movie.mpv_buffer 可配（用户可调），缺省用内置默认：
    demuxer_max_bytes=256MiB / readahead_secs=15 / pause_initial=yes /
    pause_wait=2 / network_timeout=15，demuxer-thread 固定开启。
    仅电影 video 分支生效——音乐链零影响（音乐不加缓冲参数）。"""
    b = config.get("movie.mpv_buffer", {}) or {}
    args = [
        "--demuxer-max-bytes=" + str(b.get("demuxer_max_bytes", "256MiB")),
        "--demuxer-readahead-secs=" + str(b.get("readahead_secs", 15)),
        "--cache-pause-initial=" + ("yes" if b.get("pause_initial", True) else "no"),
        "--cache-pause-wait=" + str(b.get("pause_wait", 2)),
        "--network-timeout=" + str(b.get("network_timeout", 15)),
        "--demuxer-thread=yes",
    ]
    # Optional upper selection bound for multi-variant HLS, in bits/second.
    # Absent/invalid preserves MPV's existing max selection and cache defaults.
    cap = b.get("hls_bitrate")
    if type(cap) is int and 64000 <= cap <= 100000000:
        args.append(f"--hls-bitrate={cap}")
    return args


def _serialized_media(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        guard = kwargs.pop("_voice_guard", None)
        lock = getattr(self, "_media_action_lock", None)
        if lock is None:
            lock = self._media_action_lock = threading.RLock()
        if not lock.acquire(timeout=0 if method.__name__ in ("tick", "poll_film_ui") else .2):
            return False
        try:
            if guard is not None and not guard():
                return False
            return method(self, *args, **kwargs)
        finally:
            lock.release()
    return wrapped


class MovieController:
    """搜索 + 播放控制 + 与音乐互斥（事件解耦）。"""

    def __init__(self, source_url=None, mpv_path=None, ipc_pipe=None,
                 music=None, config=None, source_manager=None,
                 voice_feedback=None, speech_queue=None):
        self._config = config or get_config()
        # P1：语音搜电影结果 → UI 反馈回调（MoviePanel 注入；None 时只 print 兜底）
        self._voice_feedback = voice_feedback
        # Presentation callback, separate from user-facing feedback/TTS text.
        self._voice_playback_ready = None
        # SpeechQueue 引用（main wiring 注入）：_notify_voice 时同时 TTS 播报，
        # 避免"请说电影名字"只刷状态栏不发声。
        self._speech_queue = speech_queue
        self.source_url = source_url or self._config.get("movie.source_url") or DEFAULT_SOURCE_URL
        # 多源（包2c后续）：构造注入 manager（测试/复用）；显式 source_url → 单源兼容；否则自建
        if source_manager is not None:
            self._source_manager = source_manager
        elif source_url:
            self._source_manager = _SingleSourceManager(source_url)
        else:
            self._source_manager = MovieSourceManager()
        # 搜索策略 C 的状态：上次命中的源名（面板提示"已从 XX 源找到"）/ 全部失败原因
        self.last_search_source = None
        self.last_search_error = ""
        self._search_local = threading.local()
        self._manual_search_lock = threading.Lock()
        self._manual_search_serial = 0
        self._manual_search_preferred = None
        # mpv 查找链（打包零配置运行）：settings 显式 → 打包内置 bundled → PATH（下游兜底）
        mpv_bin = resolve_mpv_path(mpv_path or self._config.get("music.mpv_path"))
        pipe = ipc_pipe or self._config.get("movie.mpv_ipc_pipe") or DEFAULT_IPC_PIPE
        # 断点续播目录（仅电影实例；音乐链不加——用户明确不要）
        watch_dir = (self._config.get("movie.watch_later_dir")
                     or os.path.join(self._config.get("app.data_dir", str(PROJECT_ROOT / "data")),
                                     "movie", "watch_later"))
        try:
            os.makedirs(watch_dir, exist_ok=True)
        except Exception:
            pass
        self.mpv = MpvController(mpv_bin, pipe, video=True, watch_later_dir=watch_dir,
                                 extra_video_args=_mpv_buffer_args(self._config))
        self._music = music
        self._current = None
        self._current_index = None
        # V2-2：播放会话代次，每次加载新片递增；全局暂停快照据此判断会话是否变化
        self._playback_gen: int = 0
        self._film_session = None
        self._film_phase = "stopped"
        self._film_sample = {}
        self._media_action_lock = threading.RLock()
        self._music_pause_generation = None
        self._ownership_attempts = (None, 0)
        self._voice_candidate_lock = threading.RLock()
        self._voice_candidates = ()
        self._voice_candidate_revision = 0
        self._voice_candidate_guard = lambda: False
        self._voice_selected = None
        self.voice_browse_kind = ""
        self._stopping = False
        self._pausing = False
        self._subscribed = False
        # 内嵌播放（S0+S1）：settings movie.embed（默认 true，永不 stage）。
        # true + 播放窗口注入 winId → mpv --wid 内嵌；false/无窗口 → 独立窗口（现状回退）。
        self._embed = bool(self._config.get("movie.embed", True))
        self._embed_wid = None
        # 自动连播（默认开——观影刚需；配置可关）
        self._auto_next = bool(self._config.get("movie.auto_next", True))
        # idle 上升沿检测：mpv --idle=yes 启动即 idle，初始 True 防首播误触发
        self._last_idle = True
        self._external_url_mode = False  # play() 正常剧集模式；play_external_url 置 True
        self._external_url_seen_active = False  # 外部 URL：是否已观察到 mpv 离开 idle
        history_db = self._config.get("movie.history_db") or os.path.join(
            self._config.get("app.data_dir", str(PROJECT_ROOT / "data")), "movie_history.db")
        self.history = MovieHistory(history_db)
        from .preferences import FilmMemory
        from .recommendation import FilmRecommendations
        from .discovery import MovieDiscovery
        self.film_memory = FilmMemory(Path(self.history.db_path).with_name("movie_preferences.db"))
        self.recommendations = FilmRecommendations(self, self.film_memory,
            MovieDiscovery(Path(self.history.db_path).parent / "movie" / "public_ranks.json"))
        self._play_guard_local = threading.local()
        self._recommendation_ready = None
        self._film_last_sample = None
        self._film_saved_at = 0
        self._resume_pending = None
        self._pending_history = None
        self._history_recorded_gen = None
        self._sub_searched = set()
        self._subtitle_lock = threading.RLock()
        self._pending_local_subtitle = None
        # 崩溃自愈状态（对齐音乐链 _try_restart_mpv：防重入 + 3 次/5 分钟上限）
        self._recover_lock = threading.Lock()
        self._recover_pending = False   # 一次恢复进行中（防重入）
        self._recover_count = 0         # 当前窗口内已恢复次数
        self._recover_window_ts = 0.0   # 窗口起点
        self._recover_gave_up = False   # 超限放弃（停止自愈）
        # IPC 静默检测（进程活但不响应）：宽限期 10s + 连续 10 次 None 才恢复
        self._ipc_fail_streak = 0
        self._mpv_start_ts = time.time()
        self._ipc_recover_triggered = False
        self._subscribe()
        self._register_commands()

    # ---- 语音命令注册（包2b：语音控制电影） ----
    # 电影由 MoviePanel 自建（movie=None），不依赖 main.py 注入：
    # 构造时自注册 MOVIE_* 命令 + 写播放态 → 语音链可路由到电影。

    def _register_commands(self):
        from ..core.command_bus import CommandType, get_command_bus
        try:
            bus = get_command_bus()
            bus.register(CommandType.MOVIE_SEARCH_PLAY, self._cmd_search_play)
            bus.register(CommandType.MOVIE_RECOMMEND, lambda c: self._cmd_recommend(c))
            bus.register(CommandType.MOVIE_CONTINUE, lambda c: self._cmd_recommend(c, resume=True))
            bus.register(CommandType.MOVIE_PAUSE, lambda c: self.pause(_voice_guard=c.is_current) if c.is_current else self.pause())
            bus.register(CommandType.MOVIE_RESUME, lambda c: self.resume(_voice_guard=c.is_current) if c.is_current else self.resume())
            bus.register(CommandType.MOVIE_NEXT_EPISODE, lambda c: self.next_episode())
            bus.register(CommandType.MOVIE_PREV_EPISODE, lambda c: self.prev_episode())
            bus.register(CommandType.MOVIE_STOP, lambda c: self.stop(_voice_guard=c.is_current) if c.is_current else self.stop())
            bus.register(CommandType.MOVIE_FEEDBACK, lambda c: self._cmd_feedback(c))
            bus.register(CommandType.MOVIE_VOLUME_SET, lambda c: self.set_volume(c.get("volume", 50)))
            bus.register(CommandType.MOVIE_VOLUME_UP, lambda c: self.volume_up(c.get("step", 5)))
            bus.register(CommandType.MOVIE_VOLUME_DOWN, lambda c: self.volume_down(c.get("step", 5)))
            bus.register(CommandType.MOVIE_SEEK, lambda c: self.seek(c.get("seconds", 0), c.get("mode", "relative")))
        except Exception as e:
            logger.error(f"[Movie] 注册语音命令失败: {e}")

    def _cmd_recommend(self, command, resume=False):
        from ..core.operation_result import OperationResult, OperationStatus
        from ..core.notifications import current_operation_id
        select = self.recommendations.continuing if resume else self.recommendations.recommend
        if command.get("kind") == "mixed":
            from .recommendation import FilmBatch
            rows, states = [], []
            current = command.is_current or (lambda: True)
            for kind in ("movie", "tv", "anime"):
                if not current():
                    return OperationResult(OperationStatus.UNKNOWN, "已取消过时的影视推荐")
                part, current = select(kind=kind, is_current=command.is_current,
                    **({"period": command.get("period", "")} if resume else {}))
                if not current():
                    return OperationResult(OperationStatus.UNKNOWN, "已取消过时的影视推荐")
                if not resume or part.action == "resume":
                    rows.extend(part.rows[:2])
                states.extend(part.states)
            message = ("已找到续播记录，请选择要继续观看的内容" if rows else "没有找到可靠的续播记录，请查看继续观看") if resume else ("综合影视候选：请在页面选择后搜索片源" if rows else "暂无可用影视候选，请刷新榜单或稍后重试")
            batch = FilmBatch(rows, message, "mixed", "resume" if resume else "recommend", states=states)
        else:
            batch, current = select(is_current=command.is_current, **command.params)
        presenter = self._recommendation_ready
        if not current():
            return OperationResult(OperationStatus.UNKNOWN, "已取消过时的影视推荐")
        if not callable(presenter):
            return OperationResult(OperationStatus.UNAVAILABLE, "电影页面尚未就绪，请打开电影页再试")
        presenter((batch, current, current_operation_id()))
        if not batch.rows:
            return OperationResult(OperationStatus.UNAVAILABLE, batch.message)
        return OperationResult(OperationStatus.ACCEPTED if resume else OperationStatus.CONFIRMED,
                               "续播请求已提交，正在加载或等待选择" if resume else f"已准备 {len(batch.rows)} 部候选，请在电影页选择")

    def _cmd_feedback(self, command):
        from .recommendation import item_work
        from ..core.operation_result import OperationResult, OperationStatus
        item = command.get("item")
        if not isinstance(item, dict) or command.is_current is not None and not command.is_current():
            return OperationResult(OperationStatus.UNKNOWN, "影片已变化，请重新选择")
        ok = self.film_memory.remember(item_work(item), bool(command.get("positive")))
        return OperationResult(OperationStatus.CONFIRMED if ok else OperationStatus.FAILED,
                               "已记住影视偏好" if ok else "影视偏好暂未保存")

    def publish_voice_candidates(self, rows):
        from copy import deepcopy
        import uuid
        service = self.recommendations
        generation = service._generation
        with self._voice_candidate_lock:
            self._voice_candidate_revision += 1
            self._voice_candidates = tuple(dict(deepcopy(row), _voice_candidate_id=uuid.uuid4().hex) for row in rows)
            self._voice_selected = None
            self._voice_candidate_guard = lambda: not service._closed and service._generation == generation
            return list(self._voice_candidates)

    def note_voice_candidate(self, candidate_id):
        with self._voice_candidate_lock:
            self._voice_selected = candidate_id

    def voice_candidate(self, index=None):
        with self._voice_candidate_lock:
            if not self._voice_candidate_guard():
                return None
            rows = self._voice_candidates
            row = (rows[index] if type(index) is int and 0 <= index < len(rows) else None) if index is not None else next((r for r in rows if r.get("_voice_candidate_id") == self._voice_selected), None)
            return (row["_voice_candidate_id"], self._voice_candidate_revision) if row else None

    def voice_candidate_row(self, identity):
        from copy import deepcopy
        with self._voice_candidate_lock:
            if not isinstance(identity, (tuple, list)) or len(identity) != 2 or identity[1] != self._voice_candidate_revision or not self._voice_candidate_guard():
                return None
            row = next((r for r in self._voice_candidates if r.get("_voice_candidate_id") == identity[0]), None)
            return deepcopy(row) if row else None

    def _cmd_search_play(self, command):
        """语音"播放电影XX"：搜索 → 播 Top1（语音直达，不弹 UI 选集对话框）。
        包2c：params["episode"] 直跳对应集（"播放电影甄嬛传第5集"→第5集）；
        无集数 → 播第一集（现状不变）；集数越界 → 提示 + False（不崩）。"""
        service = getattr(self, "recommendations", None)
        movie_guard = service.begin(getattr(command, "is_current", None)) if service is not None else None
        def current():
            if movie_guard is not None:
                return movie_guard()
            guard = getattr(command, "is_current", None)
            return guard is None or guard()

        def notify(message):
            if current():
                guard = getattr(command, "is_current", None)
                if guard is None:
                    self._notify_voice(message)
                else:
                    self._notify_voice(message, is_current=guard)

        def load(item, index=None):
            if not current():
                return None
            local = getattr(self, "_play_guard_local", None)
            previous = getattr(local, "current", None) if local is not None else None
            if local is not None:
                local.current = current
            try:
                ok = self.play(item) if index is None else self.play(item, index=index)
            finally:
                if local is not None:
                    local.current = previous
            if ok is False:
                notify("电影加载未成功，请尝试其他片源")
            elif ok is True and current():
                ready = getattr(self, "_voice_playback_ready", None)
                if callable(ready):
                    try:
                        ready(current if movie_guard is not None else getattr(command, "is_current", None))
                    except Exception as e:
                        logger.error("[Movie] 语音播放窗口显示请求失败: %s", e)
            return ok

        if not current():
            return None
        query = ((command.params or {}).get("query") or "").strip()
        if not query:
            notify("请说电影名字")
            return False
        episode = (command.params or {}).get("episode")
        try:
            items = self.search(query)
        except Exception as e:
            notify("语音搜索失败，请检查网络和片源")
            return False
        if not current():
            return None
        if not items:
            # P1：UI 反馈（面板状态栏显示"未找到：XX"）
            notify(f"未找到：{query}")
            return False
        item = items[0]
        if episode is not None:
            ep = int(episode)
            eps = self.episodes(item)
            if len(eps) <= 1:
                if ep == 1:
                    notify(f"正在加载：{item.get('vod_name') or ''}")
                    return load(item)
                notify(f"该片只有 1 集，没有第 {ep} 集")
                return False
            if ep < 1 or ep > len(eps):
                notify(f"该剧只有 {len(eps)} 集，没有第 {ep} 集")
                return False
            notify(f"正在加载：{item.get('vod_name') or ''} 第{ep}集")
            return load(item, index=ep - 1)
        notify(f"正在加载：{item.get('vod_name') or ''}")
        return load(item)

    def _notify_voice(self, msg: str, is_current=None):
        """语音命令结果 → UI 反馈 + TTS 播报。"""
        if is_current is not None and not is_current():
            return
        cb = getattr(self, "_voice_feedback", None)
        if callable(cb):
            try:
                cb(msg)
            except Exception as e:
                logger.error(f"[Movie] 语音反馈回调异常: {e}")
        # 同时 TTS 播报（SpeechQueue.enqueue 是线程安全的，可在 worker 线程调）
        sq = getattr(self, "_speech_queue", None)
        if sq is not None and msg:
            try:
                from app.speech.speech_queue import SpeechItem, PRIORITY_B
                import time as _t
                sq.enqueue(SpeechItem(
                    text=msg, priority=PRIORITY_B,
                    expire_at=_t.time() + 30.0,
                    category="movie_voice", interruptible=True, is_current=is_current))
            except Exception as e:
                logger.error(f"[Movie] TTS 播报失败: {e}")
        else:
            print(f"[Movie] 语音反馈: {msg}")

    def _write_state(self, playing=None, paused=None):
        """写播放态到 state_store（语音"谁在播谁响应"路由用）。
        playing=True 表示电影占着播放权（即使暂停）；paused 仅当 playing 时有意义。"""
        try:
            st = get_state_store()
            if playing is not None:
                st.set(StateNamespace.MOVIE, "playing", bool(playing))
            if paused is not None:
                st.set(StateNamespace.MOVIE, "paused", bool(paused))
        except Exception as e:
            logger.error(f"[Movie] 写播放状态失败: {e}")

    # ---- 互斥：事件解耦（音乐 → 电影） ----

    def _subscribe(self):
        try:
            bus = get_event_bus()
            # 换歌（TRACK_CHANGED）+ 继续播放（MUSIC_PLAY，音乐链 play/resume 补发）
            bus.subscribe(EventType.MUSIC_TRACK_CHANGED, self._on_music_playing)
            bus.subscribe(EventType.MUSIC_PLAY, self._on_music_playing)
            # 软件退出（main.py stop() 第一件事就 publish SYSTEM_SHUTDOWN；
            # 托盘"退出"走 stop() 不触发 aboutToQuit，只能靠这个事件收口）
            bus.subscribe(EventType.SYSTEM_SHUTDOWN, self._on_shutdown)
            self._subscribed = True
        except Exception as e:
            logger.error(f"[Movie] 订阅音乐事件失败: {e}")

    def _on_music_playing(self, event):
        """音乐开始播放（换歌或继续）→ 电影暂停（保留进度）。幂等 + 守卫防循环。"""
        self.pause()

    def _on_shutdown(self, event):
        """软件退出 → 杀电影 mpv（与音乐链 stop() 行为一致）。"""
        self.close()

    # ---- 搜索 ----

    def progressive_search_service(self):
        """Optional source-only manual UI channel; legacy voice/search stays synchronous."""
        if not isinstance(self._source_manager, MovieSourceManager):
            return None
        from .progressive_search import shared_search_service
        service = shared_search_service()
        if service is not None:
            # Conservative cached ownership only; never probe IPC on the GUI thread.
            service.configure(self._source_manager,
                              lambda: self._current is None and not self._external_url_mode)
        return service

    def search_for_ui(self, keyword, is_current, deadline):
        """Cooperative UI cancellation; reuses the original sequential search."""
        if not is_current() or time.monotonic() >= deadline:
            raise TimeoutError("search request expired")
        lock = getattr(self, "_manual_search_lock", None)
        serial = None
        if lock is not None:
            with lock:
                self._manual_search_serial += 1
                serial = self._manual_search_serial
        self._search_local.context = (is_current, deadline)
        self._search_local.manual_serial = serial
        try:
            return self.search(keyword)
        finally:
            self._search_local.context = None
            self._search_local.manual_serial = None

    def _search_current(self):
        context = getattr(getattr(self, "_search_local", None), "context", None)
        serial = getattr(getattr(self, "_search_local", None), "manual_serial", None)
        return ((serial is None or serial == self._manual_search_serial)
                and (context is None or (time.monotonic() < context[1] and context[0]())))

    def _source_search_revision(self):
        read = getattr(self._source_manager, "get_search_revision", None)
        try:
            revision = read() if callable(read) else None
            return revision if type(revision) is int else None
        except Exception:
            return None  # Optional compatibility interface: preserve legacy order.


    def _manual_source_stamp(self, sources, active, revision):
        if revision is None or self._source_search_revision() != revision:
            return None
        return (self._source_manager, revision, active,
                tuple((s.get("name"), s.get("url", "").strip()) for s in sources))


    def _manual_cooldown(self, stamp, update=None):
        """One bounded, owned observation; no networking or storage under this lock."""
        if stamp is None:
            return False
        with self._manual_search_lock:
            if not self._search_current():
                return False
            manager = self._source_manager
            current = self._manual_source_stamp(manager.get_enabled_sources(), manager.get_active_name(), stamp[1])
            if current != stamp:
                self._manual_search_preferred = None
                return False
            now = time.monotonic()
            if update == "learn":
                self._manual_search_preferred = (stamp, now + 600.0)
            elif update == "clear":
                self._manual_search_preferred = None
            known = self._manual_search_preferred
            if known is not None and (known[0] != stamp or not 0 < known[1] - now <= 600.0):
                self._manual_search_preferred = None
            return self._manual_search_preferred is not None


    def search(self, keyword, timeout=10.0):
        """苹果 CMS 多源搜索（策略 C：当前源挂了自动换，最多试 3 个启用源）。

        返回 [{vod_id, vod_name, vod_remarks, play_url, first_name, first_url, _source}]；
        命中 → last_search_source=源名；全部失败 → [] + last_search_error（面板提示）。
        不自动改 active_source（本次临时换，用户选的源保留）。"""
        if not keyword or not keyword.strip():
            return []
        if hasattr(self, "film_memory"):
            self.film_memory.record_action("search", keyword)
        manual = getattr(getattr(self, "_search_local", None), "manual_serial", None) is not None
        revision = self._source_search_revision() if manual else None
        sources = self._source_manager.get_enabled_sources()
        if not sources:
            self.last_search_error = "无可用源"
            return []
        active = self._source_manager.get_active_name()
        # Keep the user's current source first. Only use the enabled, currently
        # configured endpoint for this verified fallback; never restore an old URL.
        prioritized = ([s for s in sources if s.get("name") == active]
                       + [s for s in sources if s.get("name") == "极速资源"] + sources)
        stamp = self._manual_source_stamp(sources, active, revision) if manual else None
        preferred = self._manual_cooldown(stamp)
        if preferred:
            prioritized = [s for s in sources if s.get("name") == "极速资源"] + prioritized
            logger.info("[MovieSearch] stage=route policy=recent_connect_failure manual_serial=%s",
                        self._search_local.manual_serial)
        ordered, seen = [], set()
        for src in prioritized:
            endpoint = src.get("url", "").strip()
            if endpoint in seen:
                continue
            seen.add(endpoint)
            ordered.append(src)
            if len(ordered) == 3:
                break
        errors = []
        current_connect_failed = False
        for src in ordered:
            if not self._search_current():
                raise TimeoutError("search request expired")
            started = time.monotonic()
            try:
                items = self._search_one(src["url"], keyword, timeout)
            except Exception as e:
                if src.get("name") == active and isinstance(e, requests.ConnectTimeout):
                    current_connect_failed = True
                if preferred and src.get("name") == "极速资源":
                    self._manual_cooldown(stamp, "clear")
                logger.info("[MovieSearch] stage=source result=failed attempt=%s elapsed_ms=%s type=%s",
                            len(errors) + 1, round((time.monotonic() - started) * 1000), type(e).__name__)
                errors.append(f"{src['name']}: {type(e).__name__}")
                continue
            if not self._search_current():
                raise TimeoutError("search request expired")
            logger.info("[MovieSearch] stage=source result=returned attempt=%s elapsed_ms=%s count=%s",
                        len(errors) + 1, round((time.monotonic() - started) * 1000), len(items))
            if items:
                for it in items:
                    it["_source"] = src["name"]
                    try:
                        from .content_classification import classify_content_info
                        info = classify_content_info(it)
                        it["content_classification"] = info
                        it["content_category"] = info["category"]
                    except Exception:
                        it["content_category"] = "未知"
                if not self._search_current():
                    raise TimeoutError("search request expired")
                if (current_connect_failed and src.get("name") == "极速资源"
                        and isinstance(items, list) and all(isinstance(item, dict) for item in items)):
                    self._manual_cooldown(stamp, "learn")
                self.last_search_source = src["name"]
                self.last_search_error = ""
                return items
            if preferred and src.get("name") == "极速资源":
                self._manual_cooldown(stamp, "clear")
            errors.append(f"{src['name']}: 无结果")
        if not self._search_current():
            raise TimeoutError("search request expired")
        self.last_search_source = None
        self.last_search_error = "；".join(errors) or "无可用源"
        return []

    def _search_one(self, url, keyword, timeout):
        """单源搜索（策略 C 的单个源执行）。网络失败抛异常（上层换下一个源）。"""
        params = {"ac": "videolist", "wd": keyword.strip()}
        started = time.monotonic()
        context = getattr(getattr(self, "_search_local", None), "context", None)
        deadline = min(started + timeout, context[1]) if context else started + timeout
        if not self._search_current() or deadline <= started:
            raise requests.Timeout("search request expired")
        from .playback_diagnostics import source_tag
        tag = source_tag(url)
        serial = getattr(context[0], "request_serial", None) if context else None
        logger.info("[MovieSearch] stage=request serial=%s source_tag=%s", serial, tag)
        read_budget = min(timeout, deadline - started)
        # A connect timeout may apply to more than one resolved address. Keep
        # failed connections from spending the whole three-source UI budget.
        r = requests.get(url, params=params, timeout=(min(2., read_budget), read_budget), headers=UA,
                         stream=True)
        try:
            logger.info("[MovieSearch] stage=headers serial=%s source_tag=%s status=%s elapsed_ms=%s",
                        serial, tag, r.status_code,
                        round((time.monotonic() - started) * 1000))
            r.raise_for_status()
            body = bytearray()
            while True:
                if not self._search_current() or time.monotonic() >= deadline:
                    raise requests.Timeout("CMS response deadline exceeded")
                # read1 returns currently available bytes, unlike a full-size read
                # that a slow trickle could keep occupied indefinitely.
                try:
                    chunk = r.raw.read1(65536, decode_content=True)
                except ReadTimeoutError as exc:
                    raise requests.Timeout("CMS body read timeout") from exc
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise ValueError("CMS response too large")
            if not self._search_current() or time.monotonic() >= deadline:
                raise requests.Timeout("CMS response deadline exceeded")
            logger.info("[MovieSearch] stage=http serial=%s source_tag=%s status=%s bytes=%s elapsed_ms=%s",
                        serial, tag, r.status_code, len(body), round((time.monotonic() - started) * 1000))
            data = parse_cms_response(body.decode(r.encoding or "utf-8-sig", errors="replace"))
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError("invalid CMS response")
        finally:
            r.close()
        items = []
        for v in data.get("list", []) or []:
            pu = v.get("vod_play_url") or ""
            first = first_play_url(pu)
            items.append({
                "vod_id": v.get("vod_id"),
                "vod_name": v.get("vod_name", "") or "",
                "vod_remarks": v.get("vod_remarks", "") or "",
                "vod_pic": v.get("vod_pic") or "",  # P0-1：海报墙依赖（实测 CMS 有值、直连可下）
                "play_url": pu,
                "first_name": first[0] if first else "",
                "first_url": first[1] if first else "",
            })
        return items

    # ---- 选集 ----

    def episodes(self, item):
        """拆集：返回 [(name, url), ...]；单集/无 play_url → 空列表（调用方走直播）。"""
        return episode_eps(item)

    @staticmethod
    def is_multi_episode(item):
        """剧集判据双保险：段数 > 1 为主 + remarks 含 全?\\d+集 辅助。"""
        if len(episode_eps(item)) > 1:
            return True
        remarks = (item.get("vod_remarks") or "") if isinstance(item, dict) else ""
        return bool(re.search(r"全?\d+集", remarks))

    # ---- 内嵌播放（S0+S1） ----

    def embed_enabled(self) -> bool:
        """movie.embed 开关（true=内嵌优先；false=独立窗口现状）。"""
        return bool(self._embed)

    def set_embed_wid(self, wid):
        """播放窗口把容器 winId 注入（uint32 HWND）；None 清除（回退独立窗口）。"""
        self._embed_wid = wid

    def get_embed_wid(self):
        return self._embed_wid

    # ---- 播放 ----

    def _play_feedback(self, item, index, status, message="", *, media_url=None):
        import hashlib
        source = str(item.get("_source_id") or item.get("_source") or "unknown")
        key = repr((source, item.get("vod_id"), item.get("play_url"), item.get("first_url"), index))
        ref = hashlib.sha256(key.encode("utf8")).hexdigest()[:12]
        self._last_play_feedback = (ref, status, message)
        media_tag = hashlib.sha256(str(media_url or "").encode("utf8")).hexdigest()[:12]
        logger.info("[MoviePlay] attempt=%s generation=%s source_tag=%s candidate_tag=%s media_tag=%s episode=%s result=%s",
                    getattr(self, "_play_attempt", 0), getattr(self, "_playback_gen", 0),
                    hashlib.sha256(source.encode("utf8")).hexdigest()[:12], ref, media_tag, index, status)
        return False

    def playback_failure_message(self):
        feedback = getattr(self, "_last_play_feedback", None)
        return feedback[2] if feedback and feedback[2] else "播放未成功，请检查该片源或选择其他候选。"

    @_serialized_media
    def play(self, item, index=None):
        """播电影：启动视频 mpv 播指定集，确认实际起播后再暂停音乐。
        index=None → 取 first_url（现状不变）；index=N → 取第 N 集 URL。失败返回 False。
        内嵌：embed 开且有播放窗口注入 winId → mpv --wid 渲染进 Qt 容器；
        否则（embed 关 / 窗口未建）→ 独立 mpv 窗口（现状，安全回退）。"""
        if not isinstance(item, dict):
            return False
        if "$$$" in str(item.get("vod_play_url") or item.get("play_url") or ""):
            item = normalise_playback(item)
        self._play_attempt = getattr(self, "_play_attempt", 0) + 1
        self._play_feedback(item, index, "selected")
        resume_guard = item.get("_resume_guard") or getattr(getattr(self, "_play_guard_local", None), "current", None)
        if "_resume_guard" in item:
            item = {key: value for key, value in item.items() if key != "_resume_guard"}
        if callable(resume_guard) and not resume_guard():
            return False
        if not callable(resume_guard) and hasattr(self, "recommendations"):
            self.recommendations.cancel()
        url = item.get("first_url") or ""
        idx = 0
        eps = self.episodes(item)
        if index is not None:
            idx = index
            if 0 <= idx < len(eps):
                url = eps[idx][1]
            else:
                print(f"[Movie] 集数越界: index={index} 总集数={len(eps)}")
                return self._play_feedback(item, index, "invalid_episode", "该来源没有所选集数，请重新选择。")
        elif eps:
            idx = 0
            url = eps[0][1]
        if not url:
            return self._play_feedback(item, index, "no_address", "该来源未提供播放地址，请选择其他候选。")
        self._remember_view(force=True)
        # 音乐互斥由 tick 确认实际起播后处理；加载失败不打断音乐。
        # 内嵌 wid：embed 开且窗口已注入 → 传 --wid；否则 None（独立窗口，现状）
        wid = self._embed_wid if (self._embed and self._embed_wid) else None
        if wid is not None:
            print(f"[Movie] 内嵌播放 wid={wid}")
        # 独立视频实例：taskkill=False 不杀音乐实例；video=True 出视频窗口
        if not self.mpv.is_running():
            if not self.mpv.start(taskkill=False, wid=wid):
                return self._play_feedback(item, index, "start_failed", "播放器启动失败，请稍后重试。")
        # loadfile 三态确认（与 mpv.stop() 同契约；命令被接受≠媒体已解码/出声）：
        #   resp.error == "success" → 命令被接受，按原逻辑更新播放上下文
        #   resp is None（超时/无响应）→ 结果未确认 → 不伪造播放成功
        #   resp.error != "success" → 协议层明确失败 → 不伪造播放成功
        #   抛异常（IPC 未连接/写入失败）→ 不伪造播放成功。
        # 失败时音乐已按互斥先行暂停（既有行为，如实保留，不加回滚）。
        try:
            if callable(resume_guard) and not resume_guard():
                return False
            self._quality_transition(suspend=True)
            requested_at = time.monotonic()
            resp = self.mpv.loadfile(url)
        except Exception as e:
            logger.error("[Movie] loadfile 异常: type=%s", type(e).__name__)
            return self._play_feedback(item, index, "load_exception", "播放器加载异常，请检查该片源后重试。", media_url=url)
        if resp is None:
            logger.error("[Movie] loadfile 结果未确认（IPC 超时/无响应）")
            return self._play_feedback(item, index, "load_unknown", "播放器加载结果未确认，请勿重复点击；请查看播放窗口。", media_url=url)
        if not isinstance(resp, dict) or resp.get("error") != "success":
            err = resp.get("error") if isinstance(resp, dict) else resp
            logger.error("[Movie] loadfile 失败: error_type=%s", type(err).__name__)
            return self._play_feedback(item, index, "load_rejected", "播放器拒绝加载该地址，请选择其他候选。", media_url=url)
        # 确保新视频开始播放（不继承之前的暂停状态）
        self.mpv.set_property("pause", False)
        try:
            self.mpv._properties["pause"] = False
        except Exception:
            pass
        self._switch_unsettled = False
        self._current = item
        self._current_index = idx
        self._playback_gen += 1  # V2-2：新片加载，会话代次递增
        self._begin_film_session(resp, url)
        self._last_idle = False  # 新播放开始，等播完再触发连播
        self._external_url_mode = False  # 切回正常剧集模式
        self._external_url_seen_active = False
        self._pending_history = (dict(item), str(url))
        self._history_recorded_gen = None
        self._film_last_sample = None
        self._film_saved_at = 0
        position = item.get("_resume_position")
        self._resume_pending = ((self._playback_gen, self._current_index, float(position), 0) if not item.get("local_file")
                                and type(position) in (int, float) and position > 0 else None)
        self._write_state(playing=True, paused=False)
        self._pending_local_subtitle = None
        if item.get("local_file"):
            self._load_local_subtitle(str(url))
        self._start_playback_diagnostics("local" if item.get("local_file") else "cms",
                                         item.get("_source"), media_url=str(url),
                                         requested_at=requested_at)
        self._play_feedback(item, idx, "accepted", media_url=url)  # loadfile accepted is not decoded playback.
        return True

    @staticmethod
    def _local_subtitle(video_path: str) -> str | None:
        path = Path(video_path)
        if not path.is_file():
            return None
        priorities = {".srt": 0, ".ass": 1, ".ssa": 2, ".vtt": 3}
        try:
            candidates = [p for p in path.parent.iterdir()
                          if p.is_file() and p.suffix.casefold() in priorities]
        except OSError:
            return None
        if not candidates:
            return None

        video_stem = MovieController._subtitle_stem(path.name)
        # 完全同名永远优先；格式优先级只在同名字幕之间起作用。
        exact = [p for p in candidates
                 if MovieController._subtitle_stem(p.name) == video_stem]
        if exact:
            exact.sort(key=lambda p: priorities[p.suffix.casefold()])
            return str(exact[0])

        scored = []
        for candidate in candidates:
            score = MovieController._subtitle_similarity(video_stem,
                                                         MovieController._subtitle_stem(candidate.name))
            if score is not None:
                category, ratio, common = score
                scored.append((category, ratio, common,
                               -priorities[candidate.suffix.casefold()], candidate))
        if not scored:
            return None
        scored.sort(key=lambda value: value[:4], reverse=True)
        # 同一匹配类别下相似度过近无法安全区分，保持静默不加载；
        # 格式优先级只用于相似度已经明确区分不了的实质同等候选。
        best = scored[0]
        if len(scored) > 1:
            runner_up = scored[1]
            if runner_up[0] == best[0] and (best[1] - runner_up[1]) <= 0.05:
                return None
        return str(best[4])

    @staticmethod
    def _subtitle_stem(name: str) -> str:
        stem = Path(name).stem
        stem = unicodedata.normalize("NFKC", stem).casefold()
        return "".join(ch for ch in stem if ch.isalnum())

    @staticmethod
    def _subtitle_similarity(video_stem: str, subtitle_stem: str):
        """Return (rule priority, sequence ratio, common count), or None."""
        if not video_stem or not subtitle_stem:
            return None
        ratio = SequenceMatcher(None, video_stem, subtitle_stem).ratio()
        # 仅字符相同但顺序/整体差异很大时，不认定为同一视频。
        if ratio < 0.5:
            return None
        # 只统计英文字母；str.isalpha() 会把中文也算入字母。
        letters_a = Counter(ch for ch in video_stem if "a" <= ch <= "z")
        letters_b = Counter(ch for ch in subtitle_stem if "a" <= ch <= "z")
        common_letters = sum((letters_a & letters_b).values())
        if common_letters > 4:
            return (3, ratio, common_letters)
        digits_a = Counter(ch for ch in video_stem if ch.isdigit())
        digits_b = Counter(ch for ch in subtitle_stem if ch.isdigit())
        common_digits = sum((digits_a & digits_b).values())
        if common_digits >= 3:
            return (2, ratio, common_digits)
        text_a = Counter(ch for ch in video_stem if "\u4e00" <= ch <= "\u9fff")
        text_b = Counter(ch for ch in subtitle_stem if "\u4e00" <= ch <= "\u9fff")
        common_text = sum((text_a & text_b).values())
        if common_text > 2:
            return (1, ratio, common_text)
        return None

    def _load_local_subtitle(self, video_path: str) -> str | None:
        subtitle = self._local_subtitle(video_path)
        # loadfile 的成功响应仅表示接收请求，不能向尚未切走的旧媒体添加字幕。
        self._pending_local_subtitle = ((self._playback_gen, video_path, subtitle)
                                        if subtitle else None)
        return subtitle

    def _apply_pending_local_subtitle(self):
        pending = getattr(self, "_pending_local_subtitle", None)
        if not pending:
            return
        gen, video, subtitle = pending
        if gen != self._playback_gen or not (self._current or {}).get("local_file"):
            self._pending_local_subtitle = None
            return
        state = self.subtitle_state()
        if state is None or self._subtitle_path_key(state["media"]) != self._subtitle_path_key(video):
            return
        self._pending_local_subtitle = None  # 每次播放仅尝试一次；失败只记日志。
        if self._find_subtitle(state["tracks"], subtitle):
            return  # 保留 MPV 的语言选择和其他字幕，不再次追加/抢选。
        ok, message = self.load_local_subtitle(
            subtitle, expected_media=state["media"], enable=state["visible"])
        if not ok:
            logger.warning(f"[Movie] local subtitle load unconfirmed: {message}")

    @staticmethod
    def _subtitle_path_key(path):
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    @classmethod
    def _find_subtitle(cls, tracks, path):
        key = cls._subtitle_path_key(path)
        return next((t for t in tracks if t.get("external-filename")
                     and cls._subtitle_path_key(t["external-filename"]) == key), None)

    def subtitle_state(self):
        """现查当前媒体与字幕；None 表示不可确认，绝不使用旧轨道缓存。"""
        try:
            if not self.mpv.is_running():
                return None
            media = self.mpv.get_property("path", timeout=.5)
            tracks = self.mpv.get_property("track-list", timeout=.5)
            visible = self.mpv.get_property("sub-visibility", timeout=.5)
            sid = self.mpv.get_property("sid", timeout=.5)
            if (not isinstance(media, str) or not media or not isinstance(tracks, list)
                    or not any(isinstance(t, dict) and t.get("type") in ("video", "audio") for t in tracks)
                    or not isinstance(visible, bool) or sid is None
                    or self.mpv.get_property("path", timeout=.5) != media):
                return None
            return {"media": media, "visible": visible, "sid": sid,
                    "tracks": [dict(t) for t in tracks if isinstance(t, dict)
                               and t.get("type") == "sub" and type(t.get("id")) is int]}
        except Exception as e:
            logger.warning(f"[Movie] subtitle state unavailable: {type(e).__name__}")
            return None

    @staticmethod
    def _subtitle_response_ok(response):
        return isinstance(response, dict) and response.get("error") == "success"

    def load_local_subtitle(self, path, expected_media=None, *, enable=True):
        """加载/复用实际文件，返回 (确认成功, 路径或简短失败原因)。"""
        try:
            filename = str(Path(path).resolve())
            if not Path(filename).is_file():
                return False, "字幕文件不存在或不是文件，请重新选择"
            with open(filename, "rb") as f:
                f.read(1)  # 只检查可读性；格式/解码交给 MPV。
            with self._subtitle_lock:
                state = self.subtitle_state()
                if state is None:
                    return False, "请等待视频加载完成后再加载字幕"
                media = expected_media if expected_media is not None else state["media"]
                if state["media"] != media:
                    return False, "视频已切换，请重新打开字幕菜单"
                track = self._find_subtitle(state["tracks"], filename)
                if track is None:
                    response = self.mpv.sub_add(filename)
                    if response is None:
                        return False, "播放器未确认字幕加载结果，请检查字幕轨道"
                    if not self._subtitle_response_ok(response):
                        return False, "字幕加载失败，请检查文件格式或选择其他字幕"
                    state = self.subtitle_state()
                    if state is None or state["media"] != media:
                        return False, "字幕结果未确认，视频可能已经切换"
                    track = self._find_subtitle(state["tracks"], filename)
                    if track is None:
                        return False, "未检测到有效字幕轨道，请检查字幕文件"
                if not enable:
                    return True, filename  # 自动加载尊重用户已经关闭字幕的选择。
                ok, message = self.select_subtitle(track["id"], expected_media=media)
                return (True, filename) if ok else (False, message)
        except Exception as e:
            logger.warning(f"[Movie] local subtitle load failed: {type(e).__name__}")
            return False, "字幕无法读取或播放器无响应，请重新选择或稍后重试"

    def select_subtitle(self, sid, expected_media=None):
        try:
            with self._subtitle_lock:
                state = self.subtitle_state()
                if state is None or (expected_media is not None and state["media"] != expected_media):
                    return False, "当前视频状态已变化，请重新打开字幕菜单"
                if type(sid) is not int or not any(t["id"] == sid for t in state["tracks"]):
                    return False, "该字幕轨道已不可用，请重新选择"
                if state["sid"] != sid and not self._subtitle_response_ok(self.mpv.set_sid(sid)):
                    return False, "播放器未确认字幕轨道切换"
                if not state["visible"] and not self._subtitle_response_ok(
                        self.mpv.set_property("sub-visibility", True)):
                    return False, "播放器未确认字幕开启"
                actual = self.subtitle_state()
                if actual and actual["media"] == state["media"] and actual["sid"] == sid and actual["visible"]:
                    return True, "字幕已开启"
                return False, "字幕状态未确认，请重新打开字幕菜单检查"
        except Exception as e:
            logger.warning(f"[Movie] subtitle selection failed: {type(e).__name__}")
            return False, "字幕切换失败，请稍后重试"

    def set_subtitle_visible(self, visible, expected_media=None):
        try:
            with self._subtitle_lock:
                state = self.subtitle_state()
                if state is None or (expected_media is not None and state["media"] != expected_media):
                    return False, "当前视频状态已变化，请重新打开字幕菜单"
                if visible:
                    if not state["tracks"]:
                        return False, "当前没有字幕，请先加载本地字幕"
                    selected = next((t["id"] for t in state["tracks"] if t["id"] == state["sid"]),
                                    state["tracks"][0]["id"])
                    return self.select_subtitle(selected, expected_media=state["media"])
                if state["visible"] and not self._subtitle_response_ok(
                        self.mpv.set_property("sub-visibility", False)):
                    return False, "播放器未确认字幕关闭"
                actual = self.subtitle_state()
                if actual and actual["media"] == state["media"] and actual["visible"] is False:
                    return True, "字幕已关闭"
                return False, "字幕关闭结果未确认，请稍后检查"
        except Exception as e:
            logger.warning(f"[Movie] subtitle visibility failed: {type(e).__name__}")
            return False, "字幕状态调整失败，请稍后重试"

    def _remember_view(self, now_idle=None, *, force=False, sample=None):
        if getattr(self, "_switch_unsettled", False):
            return
        if not hasattr(self, "film_memory") or not self._current or getattr(self, "_external_url_mode", False):
            return
        from .recommendation import item_work
        try:
            idle = self.mpv.get_property("idle-active", timeout=.15) if now_idle is None else now_idle
            identity = (self._playback_gen, self._current_index)
            if idle is False:
                position = (sample.get("time-pos") if sample is not None
                            else self.mpv.get_property("time-pos", timeout=.15))
                duration = (sample.get("duration") if sample is not None
                            else self.mpv.get_property("duration", timeout=.15))
                if type(position) not in (int, float) or type(duration) not in (int, float) or duration <= 0:
                    return
                self._film_last_sample = (identity, position, duration)
                if not force and time.monotonic() - self._film_saved_at < 15:
                    return
                self.film_memory.observe(item_work(self._current), self._current, position, duration, self._current_index)
                self._film_saved_at = time.monotonic()
            elif idle is True and not self._last_idle and self._film_last_sample:
                previous, position, duration = self._film_last_sample
                if previous == identity:
                    self.film_memory.observe(item_work(self._current), self._current, duration, duration,
                                             self._current_index, ended=True)
                self._film_last_sample = None
        except Exception as exc:
            logger.warning("[Movie] preference progress unavailable: %s", type(exc).__name__)

    def _restore_online_position(self, now_idle):
        pending = getattr(self, "_resume_pending", None)
        if not pending or now_idle is not False:
            return
        generation, episode, position, attempts = pending
        if generation != self._playback_gen or episode != self._current_index:
            self._resume_pending = None
            return
        try:
            actual = self.mpv.get_property("time-pos", timeout=.15)
            if type(actual) in (int, float) and abs(actual - position) <= 3:
                self._resume_pending = None
                return
            if attempts == 0:
                self._quality_transition()
                self.mpv.seek(position, "absolute")
            self._resume_pending = (generation, episode, position, attempts + 1) if attempts < 3 else None
            if attempts == 3:
                logger.warning("[Movie] saved online position not confirmed")
        except Exception:
            self._resume_pending = None
            logger.warning("[Movie] saved online position unavailable")

    def _record_history_if_started(self, now_idle):
        if now_idle is not False or not self._pending_history:
            return
        if self._history_recorded_gen == self._playback_gen:
            return
        item, replay_value = self._pending_history
        media_type = "local" if item.get("local_file") else "online"
        key = (os.path.normcase(os.path.abspath(replay_value)) if media_type == "local"
               else str(item.get("vod_id") or replay_value))
        try:
            self.history.record(
                media_key=key, title=str(item.get("vod_name") or "未命名视频"),
                media_type=media_type, replay_value=replay_value,
                source_name=str(item.get("source_name") or ""),
                cover_url=str(item.get("vod_pic") or ""))
            self._history_recorded_gen = self._playback_gen
        except Exception as e:
            logger.warning("[Movie] history record failed: %s", type(e).__name__)

    @_serialized_media
    def play_external_url(self, url: str, title: str = "外部流") -> bool:
        """播放任意 URL（解析出来的外链，无 vod_id/剧集结构）。

        与 play() 同语义：先 pause 音乐互斥，启动 mpv，loadfile。
        不写 _current/_current_index（不是资源站条目，无连播/切集）。
        失败返回 False；成功返回 True。
        """
        if not url:
            return False
        self._remember_view(force=True)
        if hasattr(self, "recommendations"):
            self.recommendations.cancel()
        wid = self._embed_wid if (self._embed and self._embed_wid) else None
        if not self.mpv.is_running():
            if not self.mpv.start(taskkill=False, wid=wid):
                print("[Movie] mpv 启动失败（外部 URL）")
                return False
        try:
            self._quality_transition(suspend=True)
            requested_at = time.monotonic()
            resp = self.mpv.loadfile(url)
        except Exception as e:
            logger.error(f"[Movie] loadfile(外部 URL) 异常: {e!r}")
            return False
        if resp is None or (isinstance(resp, dict) and resp.get("error") != "success"):
            logger.error("[Movie] loadfile(外部 URL) 未确认/失败")
            return False
        self.mpv.set_property("pause", False)
        try:
            self.mpv._properties["pause"] = False
        except Exception:
            pass
        self._last_idle = False
        self._external_url_mode = True  # 外部 URL：播完不连播，idle 上升沿写 playing=False
        self._playback_gen = getattr(self, "_playback_gen", 0) + 1
        self._film_last_sample = None
        self._external_url_seen_active = False  # 缓冲期保护
        self._film_session = None
        self._film_phase = "loading"
        self._film_sample = {}
        self._write_state(playing=True, paused=False)
        self._start_playback_diagnostics("external", str(url), media_url=str(url),
                                         requested_at=requested_at)
        return True

    def _start_playback_diagnostics(self, mode, source=None, *, media_url=None,
                                    requested_at=None, _origin_hint=None):
        """Explicit opt-in observer; no UI-thread IPC and no player commands."""
        config = getattr(self, "_config", None)
        diagnostic = config is not None and config.get("movie.playback_diagnostics", False) is True
        process = getattr(self.mpv, "_process", None)
        pipe = getattr(self.mpv, "ipc_pipe", None)
        if process is None or not isinstance(pipe, str):
            return
        try:
            from .source_quality import media_origin
            from urllib.parse import urlsplit
            origin = _origin_hint if _origin_hint is not None else media_origin(media_url)
            safe_source = source
            if isinstance(source, str) and "://" in source:
                try:
                    safe_source = urlsplit(source).hostname
                except ValueError:
                    safe_source = None
            # One volatile observation descriptor, never a URL/title/history row.
            self._source_quality_context = (
                mode, safe_source, origin, self._playback_gen,
                getattr(self, "_current_index", None), process,
            )
            quality = self.source_quality_trial_enabled() and origin is not None
            if not diagnostic and not quality:
                return
            import json
            import weakref
            from .playback_diagnostics import MoviePlaybackMonitor
            monitor = getattr(self, "_playback_monitor", None)
            if monitor is None:
                emit = ((lambda data: logger.info(
                    "[MoviePerf] " + json.dumps(data, separators=(",", ":"))))
                    if diagnostic else (lambda data: None))
                monitor = self._playback_monitor = MoviePlaybackMonitor(emit)
            owner = weakref.ref(self)
            generation = self._playback_gen
            index = getattr(self, "_current_index", None)
            def current():
                controller = owner()
                return (controller is not None
                        and not getattr(controller, "_media_voice_closed", False)
                        and controller._playback_gen == generation
                        and getattr(controller, "_current_index", None) == index
                        and controller.mpv._process is process and process.poll() is None)
            observer = None
            if quality:
                from .source_quality import SourceQualityHistory, SourceQualitySession
                from .playback_diagnostics import source_tag
                if origin is not None:
                    history = getattr(self, "_source_quality_history", None)
                    if history is None:
                        history = self._source_quality_history = SourceQualityHistory()
                    serial = getattr(self, "_source_quality_serial", 0) + 1
                    self._source_quality_serial = serial
                    tracker = SourceQualitySession(
                        source_tag(source), origin, serial,
                        requested_at if requested_at is not None else time.monotonic(),
                        measure_startup=requested_at is not None)
                    last_log = [None, float("-inf")]
                    def observer(sample, now, epoch):
                        report = tracker.observe(sample, now, epoch)
                        controller = owner()
                        if (controller is None or not current()
                                or not controller.source_quality_trial_enabled()):
                            return
                        controller._source_quality_latest = epoch, report
                        history.update(report)
                        # Summary only: no title, URL, position, Cookie or media ID.
                        if (report.advisory_eligible or
                                (report.state != last_log[0] and now - last_log[1] >= 30)):
                            from dataclasses import asdict
                            logger.info("[MovieQuality] " + json.dumps(asdict(report), separators=(",", ":")))
                            last_log[:] = [report.state, now]
            monitor.start(pipe, generation, current, mode, source, on_sample=observer,
                          duration=14400 if observer is not None else None)
        except Exception as error:
            logger.warning(f"[MoviePerf] observer_unavailable type={type(error).__name__}")

    def _quality_transition(self, *, suspend=False):
        self._switch_control_revision = getattr(self, "_switch_control_revision", 0) + 1
        if suspend:
            self._source_quality_context = None
        monitor = getattr(self, "_playback_monitor", None)
        if monitor is not None:
            monitor.interrupt(suspend=suspend)

    def source_quality_trial_enabled(self):
        if getattr(self, "_media_voice_closed", False):
            return False
        override = getattr(self, "_source_quality_trial_override", None)
        if override is not None:
            return override
        config = getattr(self, "_config", None)
        return config is not None and config.get("movie.source_quality_enabled", False) is True

    def set_source_quality_trial(self, enabled):
        """Process-only observation permission. Never sends a player command."""
        if enabled and getattr(self, "_media_voice_closed", False):
            return False
        if getattr(self, "_source_quality_trial_override", None) is bool(enabled):
            return True
        self._source_quality_trial_override = bool(enabled)
        self._source_quality_latest = None
        monitor = getattr(self, "_playback_monitor", None)
        if monitor is not None:
            monitor.interrupt(suspend=True)
        context = getattr(self, "_source_quality_context", None)
        if context is not None:
            mode, source, origin, generation, index, process = context
            if (generation == self._playback_gen and index == self._current_index
                    and getattr(self.mpv, "_process", None) is process and process.poll() is None):
                # Reattach read-only sampling to the already accepted session.
                # A late enable is not a measurement of initial playback latency.
                self._start_playback_diagnostics(mode, source, _origin_hint=origin)
        return True

    def source_quality_snapshot(self):
        """Read-only, nonblocking; None means disabled or no collector."""
        history = getattr(self, "_source_quality_history", None)
        if history is None:
            return None
        monitor = getattr(self, "_playback_monitor", None)
        latest = getattr(self, "_source_quality_latest", None)
        now = time.monotonic()
        current = (latest[1] if latest and monitor is not None
                   and 0 <= now - latest[1].observed_at <= 5
                   and monitor.quality_current(latest[0]) else None)
        return {"current": current, "history": history.snapshot(now)}

    @_serialized_media
    def switch_verified_source(self, context, duration, permit):
        from .safe_switch import handoff
        try:
            return handoff(self, context, duration, permit)
        except (OSError, ValueError, ConnectionError):
            self._switch_unsettled = True
            return "unknown"

    # ---- 自动连播 ----

    def set_auto_next(self, enabled: bool):
        """连播开关（面板反映；关闭后播完不再切下一集）。"""
        self._auto_next = bool(enabled)

    def is_auto_next(self) -> bool:
        return self._auto_next

    def _pause_music_after_start(self, idle):
        if idle is not False or getattr(self, "_music", None) is None or getattr(self, "_media_voice_closed", False):
            return
        worker = getattr(self, "_ownership_worker", None)
        if worker is not None and worker.is_alive():
            return
        generation = self._playback_gen
        previous, attempts = getattr(self, "_ownership_attempts", (None, 0))
        attempts = attempts if previous == generation else 0
        if getattr(self, "_music_pause_generation", None) == generation or attempts >= 3:
            return
        self._ownership_attempts = generation, attempts + 1
        self._music_pause_generation = generation
        def pause_music():
            confirmed = False
            try:
                if self.mpv.get_property("pause", timeout=.15) is not False:
                    return
                music = self._music
                request = getattr(music, "_play_request_id", None)
                def current():
                    return (self._playback_gen == generation and self._last_idle is False
                        and not getattr(self, "_media_voice_closed", False)
                        and not self._stopping and (self._current is not None or self._external_url_mode)
                        and getattr(music, "_play_request_id", None) == request)
                if current():
                    value = music.pause(_voice_guard=current)
                    confirmed = value is not False and current() and music.mpv.get_property("pause", timeout=.15) is True
            except Exception:
                logger.warning("[Movie] music pause after confirmed start unavailable", exc_info=True)
            finally:
                if not confirmed and self._playback_gen == generation:
                    self._music_pause_generation = None
        self._ownership_worker = threading.Thread(target=pause_music, name="movie-start-ownership", daemon=True)
        self._ownership_worker.start()

    @_serialized_media
    def tick(self, current=None):
        """Collect playback state and advance only a confirmed native EOF.
        Legacy non-native adapters retain their idle polling contract."""
        if getattr(self, "_switch_unsettled", False):
            return
        self._apply_pending_local_subtitle()
        # 外部 URL 模式：播完 idle 上升沿 → 写 playing=False，不连播。
        # 缓冲期保护：loadfile 成功 ≠ 媒体已播放，先观察到 idle=False 才认可"正在播"。
        if getattr(self, "_external_url_mode", False):
            now_idle = self.mpv.get_idle_active()
            self._pause_music_after_start(now_idle)
            if not self._external_url_seen_active:
                # 尚未观察到播放：idle=False → 标记已开始；idle=True 继续等（缓冲）
                if not now_idle:
                    self._external_url_seen_active = True
            elif now_idle and not self._last_idle:
                # 已开始播放后出现 idle 上升沿 → 播完
                self._external_url_mode = False
                self._external_url_seen_active = False
                self._write_state(playing=False, paused=False)
            self._last_idle = now_idle
            return
        if self._current is None:
            self._last_idle = self.mpv.get_idle_active()
            return
        if not self.mpv.is_running():
            return
        if self.native_film_state():
            self._tick_film_session(current)
            return
        now_idle = self.mpv.get_idle_active()
        self._pause_music_after_start(now_idle)
        self._restore_online_position(now_idle)
        self._remember_view(now_idle)
        self._record_history_if_started(now_idle)
        # Watching history is independent of automatic episode advancement.
        # A user who disables auto-next still needs the existing resume entry.
        if not self._auto_next:
            self._last_idle = now_idle
            return
        # IPC 静默检测：None = 进程活但 IPC 无响应
        if now_idle is None:
            self._ipc_fail_streak += 1
            if (not self._ipc_recover_triggered
                    and (time.time() - self._mpv_start_ts) > 10.0
                    and self._ipc_fail_streak >= 10
                    and self.mpv.is_running()):
                self._ipc_recover_triggered = True
                logger.warning("[Movie] IPC 连续 10 次无响应，触发重启")
                self.restart_current()
        else:
            self._ipc_fail_streak = 0
        if now_idle and not self._last_idle:
            # 播完一集（idle 上升沿）→ 连播下一集
            self._advance_episode()
        self._last_idle = now_idle

    def native_film_state(self):
        return callable(getattr(type(self.mpv), "film_end_reason", None))

    def playback_phase(self):
        return self._film_phase

    def _begin_film_session(self, response, url):
        data = response.get("data")
        entry = data.get("playlist_entry_id") if isinstance(data, dict) else None
        entry = entry if type(entry) is int else None
        self._film_session = dict(generation=self._playback_gen, entry=entry, url=url,
                                  started_at=time.monotonic(),
                                  process=getattr(self.mpv, "_process", None),
                                  previous=None, advanced=False)
        self._film_phase = "loading"
        self._film_sample = {}

    def _accept_episode(self, response, url):
        self._playback_gen += 1
        self._begin_film_session(response, url)
        self._pending_history = (dict(self._current), str(url))
        self._film_last_sample = self._resume_pending = None
        self._film_saved_at = 0

    def _finish_film_session(self, phase):
        self._film_phase = phase
        self._film_session = None
        self._film_sample = {}
        self._pending_history = self._resume_pending = self._film_last_sample = None
        self._current = self._current_index = None
        self._last_idle = True
        self._quality_transition(suspend=True)
        self._write_state(playing=False, paused=False)

    def _tick_film_session(self, current=None):
        from .playback_diagnostics import read_sample
        session = getattr(self, "_film_session", None)
        if (not session or session["generation"] != self._playback_gen
                or session["process"] is not self.mpv._process):
            return
        sample = read_sample(self.mpv.ipc_pipe, expected_path=session["url"],
                             expected_entry=session["entry"])
        if getattr(self, "_media_voice_closed", False) or (current is not None and not current()):
            return
        if sample and sample.get("idle-active") is False:
            self._film_sample = sample
            pos = sample.get("time-pos")
            prior = session["previous"]
            moving = all(sample.get(k) is False for k in ("pause", "seeking", "paused-for-cache", "core-idle"))
            now = time.monotonic()
            if moving and pos is not None:
                if prior is not None:
                    previous, sampled_at = prior
                    speed = sample.get("speed") or 1
                    if .02 < pos - previous <= (now - sampled_at) * speed + .5:
                        session["advanced"] = True
                session["previous"] = pos, now
            else:
                session["previous"] = None
            self._restore_online_position(False)
            if session["advanced"]:
                self._film_phase = ("buffering" if sample.get("paused-for-cache") is True else
                                    "paused" if sample.get("pause") is True else "playing")
                self._last_idle = False
                self._pause_music_after_start(False)
                self._remember_view(False, sample=sample)
                self._record_history_if_started(False)
        reason = self.mpv.film_end_reason(session["entry"])
        if getattr(self, "_media_voice_closed", False) or (current is not None and not current()):
            return
        if reason is None or reason == "redirect":
            if (not sample and time.monotonic() - session["started_at"] >= 15
                    and read_sample(self.mpv.ipc_pipe, expected_path="", allow_idle=True).get("idle-active") is True):
                self._finish_film_session("unconfirmed")
            return
        # MPV can report EOF for a truncated stream. Require the last validated
        # sample to be near its declared end before advancing or marking watched.
        last = self._film_sample
        pos, duration = last.get("time-pos"), last.get("duration")
        tolerance = min(3., max(.75, (last.get("speed") or 1)*.75))
        reached_end = (session["advanced"] and pos is not None and duration is not None
                       and duration > 0 and -.5 <= duration-pos <= tolerance)
        if reason == "eof" and reached_end:
            self._remember_view(True)
            if self._auto_next and self._current_index + 1 < len(self.episodes(self._current)):
                if not self._advance_episode():
                    self._finish_film_session("failed")
            else:
                self._finish_film_session("ended")
        else:
            self._finish_film_session("stopped" if reason in ("stop", "quit") else "failed")

    @_serialized_media
    def poll_film_ui(self, current=None):
        """Called by the Qt poll worker. The controller owns the read and tick."""
        if getattr(self, "_media_voice_closed", False):
            return None
        self.tick(current)
        sample = getattr(self, "_film_sample", {})
        if self._external_url_mode and self.mpv.is_running():
            from .playback_diagnostics import read_sample
            sample = read_sample(self.mpv.ipc_pipe)
        return dict(generation=self._playback_gen, item=self._current, index=self._current_index,
                    phase=self._film_phase, sample=sample,
                    crashed=self.is_crashed(), playing=self.is_playing())

    def _advance_episode(self):
        """切到下一集；无下一集 → 末集停（不循环）。返回 True=已切，False=已停/加载失败（不推进索引）。"""
        item = self._current
        if item is None:
            return False
        eps = self.episodes(item)
        nxt = (self._current_index or 0) + 1
        if nxt >= len(eps):
            print(f"[Movie] 末集播完，停止（不循环）: {item.get('vod_name')}")
            self._current = None
            self._current_index = None
            self._last_idle = True
            self._write_state(playing=False, paused=False)
            return False
        name, url = eps[nxt]
        print(f"[Movie] 自动连播下一集: {name}")
        # loadfile 三态确认（与 play()/mpv.stop() 同契约）：命令被接受才推进
        # _current_index；error / None（未确认）/ 异常 → 不把失败集标记为当前集，
        # 不写任何播放状态（tick 随后钉住 _last_idle，失败集不会被无限重试）。
        try:
            self._quality_transition(suspend=True)
            requested_at = time.monotonic()
            resp = self.mpv.loadfile(url)
        except Exception as e:
            logger.error(f"[Movie] 自动连播 loadfile 异常: {e!r}")
            return False
        if resp is None:
            logger.error("[Movie] 自动连播 loadfile 结果未确认（IPC 超时/无响应）")
            return False
        if not isinstance(resp, dict) or resp.get("error") != "success":
            err = resp.get("error") if isinstance(resp, dict) else resp
            logger.error(f"[Movie] 自动连播 loadfile 失败: error={err}")
            return False
        self._current_index = nxt
        self._accept_episode(resp, url)
        self._start_playback_diagnostics("cms", item.get("_source"), media_url=str(url),
                                         requested_at=requested_at)
        return True

    @_serialized_media
    def next_episode(self) -> bool:
        """语音切下一集；保留切换前的暂停状态。"""
        was_paused = False
        try:
            if self.mpv.is_running():
                was_paused = bool(self.mpv.get_property("pause", timeout=0.2))
        except Exception:
            pass
        ok = self._advance_episode()
        if ok and was_paused:
            try:
                self.mpv.set_property("pause", True)
            except Exception as e:
                logger.warning(f"[Movie] 下一集已切换但恢复暂停失败: {e!r}")
        return ok

    @_serialized_media
    def prev_episode(self) -> bool:
        """语音切上一集；首集返回 False。"""
        item = self._current
        if item is None:
            return False
        cur = self._current_index or 0
        if cur <= 0:
            logger.info("[Movie] 已是首集，无法切上一集")
            return False
        prev = cur - 1
        eps = self.episodes(item)
        if prev >= len(eps):
            return False
        name, url = eps[prev]
        try:
            self._quality_transition(suspend=True)
            requested_at = time.monotonic()
            resp = self.mpv.loadfile(url)
        except Exception as e:
            logger.error(f"[Movie] 切上一集 loadfile 异常: {e!r}")
            return False
        if resp is None:
            return False
        if not isinstance(resp, dict) or resp.get("error") != "success":
            return False
        self._current_index = prev
        self._accept_episode(resp, url)
        self._start_playback_diagnostics("cms", item.get("_source"), media_url=str(url),
                                         requested_at=requested_at)
        logger.info(f"[Movie] 切到上一集: {name}")
        return True

    def _save_local_progress(self):
        """Best-effort native checkpoint while the local media is still loaded."""
        item = self._current
        if not isinstance(item, dict) or not item.get("local_file"):
            return
        try:
            if not self.mpv.is_running():
                return
            result = self.mpv.write_watch_later_config(str(item.get("first_url") or ""), timeout=1.0)
            if result is True:
                logger.info("[Movie] 本地电影进度已保存")
            elif result is not False:
                logger.error("[Movie] 本地电影进度保存未确认，继续停止/退出")
        except Exception as e:
            logger.error(f"[Movie] 本地电影进度保存失败，继续停止/退出: {type(e).__name__}: {e}")

    @_serialized_media
    def stop(self):
        """停电影（幂等；_stopping 守卫防互斥循环）。

        三态语义（与 mpv.stop() 返回值对应，参照音乐链 stop_current）：
        - 明确成功（True）→ 清播放上下文 + 写停止状态；
        - 明确失败（False）/ 结果未确认（None）/ 抛异常 → 保留播放上下文与
          原有 StateStore 播放状态，仅钉死 _last_idle=True 防旧 idle 基线引发
          自动连播，记录错误日志——绝不伪装停止成功；
        - MPV 本就未运行 → 无进程可停，按既有语义幂等清理播放上下文。
        """
        if self._stopping:
            return None
        self._quality_transition(suspend=True)
        self._remember_view(force=True)
        if hasattr(self, "recommendations"):
            self.recommendations.cancel()
        self._stopping = True
        try:
            if self.mpv.is_running():
                self._save_local_progress()
                try:
                    result = self.mpv.stop()  # True=成功 / False=错误响应 / None=未确认
                except Exception as e:
                    logger.error(f"[Movie] stop: mpv.stop() 失败，停止状态未确认: {e!r}")
                    self._last_idle = True
                    return False
                if result is True:
                    self._current = None
                    self._current_index = None
                    self._last_idle = True
                    self._write_state(playing=False, paused=False)
                else:
                    # False：MPV 明确报告失败；None：IPC 超时/管道失效/无响应
                    # （命令可能已执行或未执行）。不能确认停止成功 → 保留播放
                    # 上下文与状态，轮询按 mpv 真实状态自行收敛；_last_idle=True
                    # 防止旧基线把停止误判为自然播完而自动连播。
                    self._last_idle = True
                    if result is False:
                        logger.error("[Movie] stop: mpv.stop() 返回错误响应，停止未执行，保留播放上下文")
                    else:
                        logger.error("[Movie] stop: mpv.stop() 结果未确认（IPC 超时/管道失效/无响应），保留播放上下文")
                    return False
            else:
                # MPV 本就未运行：无进程可停，幂等清理播放上下文（既有语义，保持不变）
                self._current = None
                self._current_index = None
                self._last_idle = True
                self._write_state(playing=False, paused=False)
            self._switch_unsettled = False
            self._film_phase = "stopped"
            self._film_session = None
            self._film_sample = {}
            return True
        finally:
            self._stopping = False

    # ---- 崩溃自愈（S8：mpv 崩 → 重启 + wid 重传 + loadfile 续播；对齐音乐链 _try_restart_mpv） ----

    def is_crashed(self) -> bool:
        """mpv 进程死且仍有播放上下文 → 崩溃（可触发自愈）。
        正常停播（播完末集/用户停止）进程活着且 _current 已清 → False。"""
        return self._current is not None and not self.mpv.is_running()

    def is_gave_up(self) -> bool:
        """恢复超限已放弃（3 次/5 分钟窗口内仍崩）。"""
        return self._recover_gave_up

    def restart_current(self, wid=None) -> bool:
        """mpv 崩溃自愈入口（播放窗口 _tick 检测到 is_crashed 后调用）。

        锁内只做防重入/上限决策（<1ms），实际重启在 daemon 线程
        （stop_process + start 最坏 ~4s，不阻塞 UI——音乐链教训）。
        恢复链：stop_process → start(taskkill=False, wid=wid)（新进程重传 --wid）
        → loadfile 当前集 URL → seek 回崩溃前位置（续播，位置取最后一次成功轮询缓存）。
        返回 True=已调度恢复；False=超限放弃或无可恢复上下文（调用方直接收尾）。
        """
        self._switch_unsettled = False
        self._quality_transition(suspend=True)
        self._ipc_fail_streak = 0
        self._mpv_start_ts = time.time()
        self._ipc_recover_triggered = False
        with self._recover_lock:
            if self._recover_pending:
                return True            # 已在恢复中（防重入：不重复起线程）
            if self._current is None:
                return False
            now = time.time()
            if now - self._recover_window_ts > 300:
                self._recover_window_ts = now
                self._recover_count = 0
            if self._recover_count >= 3:
                # 超限放弃：清播放上下文（is_crashed → False，UI 不再触发）并通知
                if not self._recover_gave_up:
                    self._recover_gave_up = True
                    print("[Movie] mpv 多次重启失败，放弃自动恢复（停止自愈）")
                    self._current = None
                    self._current_index = None
                    self._last_idle = True
                    self._write_state(playing=False, paused=False)
                return False
            self._recover_count += 1
            print(f"[Movie] mpv 异常，尝试自动恢复 {self._recover_count}/3")
            self._recover_pending = True
            # 快照（重启完成时用户可能已停/切集——worker 用快照对比，不恢复旧上下文）
            item = self._current
            idx = self._current_index or 0
            eps = self.episodes(item)
            url = eps[idx][1] if 0 <= idx < len(eps) else (item.get("first_url") or "")
            if self.native_film_state():
                pos = getattr(self, "_film_sample", {}).get("time-pos") or self.mpv.get_current_time()
            else:
                pos = self.get_position()
            wid_snap = wid if wid is not None else self._embed_wid
        threading.Thread(
            target=self._restart_worker,
            args=(item, idx, url, pos, wid_snap),
            daemon=True,
            name="movie-mpv-restart",
        ).start()
        return True

    def _restart_worker(self, item, idx, url, pos, wid):
        """后台执行实际重启 + 恢复；任何失败不抛（崩溃恢复失败不崩 Qt）。"""
        try:
            try:
                self.mpv.stop_process()
            except Exception:
                pass
            try:
                ok = bool(self.mpv.start(taskkill=False, wid=wid,
                                         pipe_timeout=3.0, prop_timeout=0.5))
            except Exception as e:
                logger.error(f"[Movie] mpv 重启异常: {e!r}")
                ok = False
        finally:
            with self._recover_lock:
                self._recover_pending = False
        if not ok:
            print("[Movie] mpv 重启失败，等待下次轮询")
            return
        # 关窗中断守卫：恢复完成时上下文已变（用户停止/切集）→ 放弃恢复
        if self._current is not item or (self._current_index or 0) != idx:
            if self._current is None:
                try:
                    self.mpv.stop_process()   # 已停止且无新播放 → 回收刚起的进程
                except Exception:
                    pass
            return
        try:
            resp = self.mpv.loadfile(url)
        except Exception as e:
            logger.error(f"[Movie] 崩溃恢复 loadfile 异常: {e!r}")
            return
        if resp is None:
            logger.error("[Movie] 崩溃恢复 loadfile 结果未确认（IPC 超时/无响应）")
            return
        if not isinstance(resp, dict) or resp.get("error") != "success":
            err = resp.get("error") if isinstance(resp, dict) else resp
            logger.error(f"[Movie] 崩溃恢复 loadfile 失败: error={err}")
            return
        try:
            if pos and pos > 0:
                self.mpv.seek(pos, "absolute")
        except Exception as e:
            logger.error(f"[Movie] 崩溃恢复 seek 异常: {e!r}")
            return
        self._begin_film_session(resp, url)
        self._last_idle = False      # 恢复播放：重置连播基线（防误判 idle 上升沿）
        self._write_state(playing=True, paused=False)
        print("[Movie] mpv 崩溃恢复完成（wid 重传 + loadfile 续播）")

    # ---- 播放控制（复用 mpv JSON IPC） ----

    @_serialized_media
    def pause(self):
        """暂停电影（保留进度；_pausing 守卫防互斥循环）。"""
        if self._pausing:
            return
        self._pausing = True
        try:
            if (self._current is not None or self._external_url_mode) and self.mpv.is_running():
                self._quality_transition()
                self.mpv.pause()
                if self.mpv.get_property("pause", timeout=0.5) is not True:
                    return None
                # 仍占播放权（playing=True），标记暂停态（语音"继续"可恢复）
                self._write_state(paused=True)
                self._notify_voice("电影已暂停")
                return True
            return False
        finally:
            self._pausing = False

    def pause_and_confirm(self, timeout: float = 1.0) -> bool:
        """暂停并确认 mpv 已进入暂停态。返回 True=确认暂停成功。"""
        if self._pausing:
            return True
        self._pausing = True
        try:
            if self._current is None or not self.mpv.is_running():
                return True  # 电影未运行，视为已暂停
            self._quality_transition()
            self.mpv.pause()
            self._write_state(paused=True)
            # 轮询确认
            import time as _t
            deadline = _t.perf_counter() + timeout
            while _t.perf_counter() < deadline:
                try:
                    p = self.mpv.get_property("pause", timeout=0.2)
                    if p is True:
                        return True
                except Exception:
                    pass
                _t.sleep(0.05)
            return False
        except Exception:
            return False
        finally:
            self._pausing = False

    def toggle_pause(self):
        """切换暂停并同步共享播放状态；返回 True=已暂停、False=已继续。"""
        self._quality_transition()
        try:
            paused = self.mpv.get_property("pause", timeout=0.5)
        except Exception:
            paused = None
        if paused is True:
            self.mpv.play()
            if self._current is not None:
                self._write_state(playing=True, paused=False)
            return False
        if paused is False:
            self.mpv.pause()
            if self._current is not None:
                self._write_state(playing=True, paused=True)
            return True
        # 兼容无法查询 pause 属性的替身/旧接口，但此时不能伪造状态。
        self.mpv.toggle_pause()
        return None

    @_serialized_media
    def resume(self):
        if (self._current is None and not self._external_url_mode) or not self.mpv.is_running():
            return False
        self._quality_transition()
        self.mpv.play()
        if self.mpv.get_property("pause", timeout=0.5) is not False:
            return None
        if self._current is not None or self._external_url_mode:
            self._write_state(playing=True, paused=False)
            self._notify_voice("电影继续播放")
        return True

    def seek(self, seconds, mode="relative"):
        self._quality_transition()
        try:
            dur = self.get_duration()
            if dur and dur > 0:
                self.mpv.seek(seconds, mode)
        except Exception:
            pass

    def get_position(self) -> float:
        return self.mpv.get_live_playback_time()

    def get_duration(self) -> float:
        return self.mpv.get_duration()

    def is_playing(self) -> bool:
        return self.mpv.is_running() and self._current is not None

    def current(self):
        return self._current

    def current_item(self):
        """当前播放条目（面板字幕按钮取片名用）。"""
        return self._current

    def get_history(self, limit=50):
        return self.history.list(limit)

    def delete_history(self, row_id):
        return self.history.delete(row_id)

    def history_item(self, row):
        """将历史记录转换为现有 MoviePanel 播放入口可接受的条目。"""
        if not isinstance(row, dict):
            return None
        value = str(row.get("replay_value") or "")
        if not value or (row.get("media_type") == "local" and not os.path.isfile(value)):
            return None
        return {"vod_name": row.get("title") or "未命名视频",
                "first_url": value,
                "local_file": row.get("media_type") == "local",
                "vod_id": row.get("media_key") or ""}

    def play_history_item(self, row):
        """兼容旧调用者；UI 播放优先使用 history_item + _play_item。"""
        item = self.history_item(row)
        return bool(item and self.play(item))

    def close(self):
        self._media_voice_closed = True
        monitor = getattr(self, "_playback_monitor", None)
        if monitor is not None:
            monitor.close()
        worker = getattr(self, "_ownership_worker", None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(.25)
        self._remember_view(force=True)
        self._recommendation_ready = None
        if hasattr(self, "recommendations"):
            self.recommendations.cancel(close=True)
        if self._subscribed:
            try:
                bus = get_event_bus()
                bus.unsubscribe(EventType.MUSIC_TRACK_CHANGED, self._on_music_playing)
                bus.unsubscribe(EventType.MUSIC_PLAY, self._on_music_playing)
                bus.unsubscribe(EventType.SYSTEM_SHUTDOWN, self._on_shutdown)
            except Exception:
                pass
            self._subscribed = False
        self._save_local_progress()
        try:
            self.mpv.stop_process()
        except Exception:
            pass
        self._write_state(playing=False, paused=False)

    # ---- 字幕（Assrt） ----

    def subtitle_token(self) -> str:
        """Assrt Token（settings.yaml movie.assrt_token，永不 stage）。"""
        return str(self._config.get("movie.assrt_token") or "").strip()

    def _assrt_proxies(self):
        """读 movie.assrt_proxy 配置；默认沿用历史 PROXY；空串 → 直连（返回 None）。"""
        try:
            p = str(self._config.get("movie.assrt_proxy", PROXY) or "").strip()
        except Exception:
            p = PROXY
        return {"http": p, "https": p} if p else None

    def subtitle_dir(self) -> str:
        d = (self._config.get("movie.subtitle_dir")
             or os.path.join(self._config.get("app.data_dir", str(PROJECT_ROOT / "data")),
                             "movie", "subtitles"))
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def search_subtitle(self, keyword, timeout=10.0):
        """Assrt 搜索字幕。返回 [{id, native_name, lang_desc, subtype, vote_score}]；
        无 Token → []；网络/解析失败 → []（不抛，UI 降级提示）。"""
        token = self.subtitle_token()
        if not token or not keyword or not keyword.strip():
            return []
        try:
            headers = dict(UA, Authorization=f"Bearer {token}")
            r = requests.get(f"{ASSRT_BASE}/search",
                             params={"q": keyword.strip(), "cnt": 15, "pos": 0},
                             headers=headers, timeout=timeout,
                             proxies=self._assrt_proxies())
            data = parse_cms_response(r.text)
            if data.get("status") != 0:
                return []
            subs = ((data.get("sub") or {}).get("subs") or []) if isinstance(data.get("sub"), dict) else []
            out = []
            for s in subs:
                lang = s.get("lang") or {}
                out.append({
                    "id": s.get("id"),
                    "native_name": s.get("native_name") or "",
                    "lang_desc": (lang.get("desc") or "") if isinstance(lang, dict) else "",
                    "subtype": s.get("subtype") or "",
                    "vote_score": s.get("vote_score") or 0,
                })
            return out
        except Exception as e:
            logger.error(f"[Movie] 字幕搜索失败: {e}")
            return []

    def load_subtitle(self, sub_id, timeout=30.0):
        """下载并加载字幕：detail → 下载 → 解压（ZIP/RAR/直接）→ 过滤假字幕 →
        存 data/movie/subtitles/ → mpv.sub_add → 强制 sid 选中。
        返回 (True, 字幕文件路径) 或 (False, 失败原因)。"""
        token = self.subtitle_token()
        if not token:
            return False, "未配置字幕 Token"
        try:
            headers = dict(UA, Authorization=f"Bearer {token}")
            proxies = self._assrt_proxies()
            r = requests.get(f"{ASSRT_BASE}/detail", params={"id": sub_id},
                             headers=headers, timeout=timeout, proxies=proxies)
            data = parse_cms_response(r.text)
            if data.get("status") != 0:
                return False, "字幕详情获取失败"
            subs = ((data.get("sub") or {}).get("subs") or []) if isinstance(data.get("sub"), dict) else []
            if not subs or not subs[0].get("url"):
                return False, "该条目无下载地址"
            url = subs[0]["url"]
            dl = requests.get(url, headers=UA, timeout=timeout, proxies=proxies)
            if dl.status_code != 200 or not dl.content:
                return False, "字幕下载失败"
            extracted = self._extract_subtitle(dl.content, subs[0].get("filename") or "")
            if not extracted:
                return False, "压缩包内无有效字幕（可能是网盘分享链接）"
            # 取第一个有效字幕落盘
            name, content = extracted[0]
            safe = "".join(c for c in name if c.isalnum() or c in "._- ")[:60].strip() or "sub"
            out_path = os.path.join(self.subtitle_dir(), f"{sub_id}_{safe}")
            try:
                with open(out_path, "wb") as f:
                    f.write(content)
            except Exception as e:
                return False, f"字幕写入失败: {e}"
            if not self.mpv.is_running():
                return False, "播放器未运行，字幕已保存（下次播放时可用）"
            return self.load_local_subtitle(out_path)
        except Exception as e:
            logger.error(f"[Movie] 字幕加载失败: {e}")
            return False, f"字幕加载失败: {e}"

    def _extract_subtitle(self, data: bytes, filename_hint: str = ""):
        """从下载字节提取字幕文件。返回 [(name, content), ...]（已过滤假字幕）。
        PK → zipfile（GBK 文件名修复 cp437→gbk）；Rar! → Windows tar 解压；其他 → 直接判。
        """
        if not data:
            return []
        if data[:2] == b"PK":
            return self._extract_zip(data)
        if data[:4] == b"Rar!":
            return self._extract_rar(data)
        # 直接字幕文件（assrt 偶发）
        if self._is_real_subtitle(data):
            return [(filename_hint or "subtitle.srt", data)]
        return []

    @staticmethod
    def _is_real_subtitle(content: bytes) -> bool:
        """假字幕过滤：网盘链接说明（pan.baidu.com/百度网盘）→ 假；
        含 srt 时间轴（-->）或 ass 头（[Script Info]）→ 真。"""
        if not content:
            return False
        low = content[:2048]
        if any(m in low for m in _FAKE_SUB_MARKERS):
            return False
        return any(m in low for m in _REAL_SUB_MARKERS)

    @staticmethod
    def _fix_zip_name(raw: str, flag_bits: int) -> str:
        """zipfile 无 UTF-8 flag 时按 cp437 解码中文名 → 用 GBK 重解（已实测坑）。
        UTF-8 flag 置位（0x800）→ 原名直接用。"""
        if not (flag_bits & 0x800):
            try:
                return raw.encode("cp437").decode("gbk")
            except Exception:
                return raw
        return raw

    def _extract_zip(self, data: bytes):
        """zipfile 解压：GBK 文件名修复 + 假字幕过滤。返回 [(name, bytes), ...]"""
        out = []
        try:
            import io
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    name = self._fix_zip_name(info.filename, info.flag_bits)
                    if not name.lower().endswith((".srt", ".ass", ".vtt", ".txt")):
                        continue
                    try:
                        content = z.read(info)
                    except Exception:
                        continue
                    if self._is_real_subtitle(content):
                        out.append((name, content))
        except Exception as e:
            logger.error(f"[Movie] ZIP 解压失败: {e}")
        return out

    def _extract_rar(self, data: bytes):
        """RAR 解压：Windows 自带 tar（libarchive 3.5.2 支持 RAR5——已实测）。
        tar 缺失/失败/超时 → 返回 []（UI 提示换条目）。"""
        tar = shutil.which("tar")
        if not tar:
            print("[Movie] 未找到 tar.exe，无法解压 RAR 字幕")
            return []
        tmp_rar = None
        tmp_dir = None
        try:
            fd, tmp_rar = tempfile.mkstemp(suffix=".rar")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            tmp_dir = tempfile.mkdtemp(prefix="movie_sub_")
            subprocess.run([tar, "-xf", tmp_rar, "-C", tmp_dir],
                           timeout=15, capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)  # 关黑窗：tar 解压不弹控制台
            out = []
            for root, _dirs, files in os.walk(tmp_dir):
                for fn in files:
                    if not fn.lower().endswith((".srt", ".ass", ".vtt")):
                        continue
                    try:
                        with open(os.path.join(root, fn), "rb") as f:
                            content = f.read()
                    except Exception:
                        continue
                    if self._is_real_subtitle(content):
                        out.append((fn, content))
            return out
        except subprocess.TimeoutExpired:
            print("[Movie] RAR 解压超时")
            return []
        except Exception as e:
            logger.error(f"[Movie] RAR 解压失败: {e}")
            return []
        finally:
            if tmp_rar:
                try:
                    os.remove(tmp_rar)
                except Exception:
                    pass
            if tmp_dir:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass

    def _force_select_sub(self):
        """sub-add 后 selected 可能 False（已实测）→ track-list 找最新字幕轨强制 sid。"""
        try:
            tracks = self.mpv.get_track_list()
            sub_ids = [t.get("id") for t in tracks
                       if t.get("type") == "sub" and t.get("id") is not None]
            if sub_ids:
                self.mpv.set_sid(max(sub_ids))
        except Exception as e:
            logger.error(f"[Movie] 字幕选中失败: {e}")

    def maybe_mark_sub_searched(self, item):
        """记录某片已搜过字幕（自动搜去重用，每片一次）。"""
        vid = (item or {}).get("vod_id")
        if vid is not None:
            self._sub_searched.add(vid)

    def sub_searched(self, item) -> bool:
        vid = (item or {}).get("vod_id")
        return vid in self._sub_searched

    # ---- 播放控制扩展（面板按钮 → mpv 属性） ----

    def set_fullscreen(self, on=True):
        if self.mpv.is_running():
            self.mpv.set_property("fullscreen", bool(on))

    def set_speed(self, rate):
        if self.mpv.is_running():
            self.mpv.set_property("speed", float(rate))

    def set_volume(self, vol):
        if not self.mpv.is_running():
            return False
        clamped = max(0, min(100, int(vol)))
        self.mpv.set_property("volume", clamped)
        try:
            from ..core.state_store import StateNamespace
            self.state_store.set(StateNamespace.MOVIE, "volume", clamped)
        except Exception:
            pass

    def get_volume(self):
        try:
            if self.mpv.is_running():
                v = self.mpv.get_property("volume", timeout=0.5)
                if v is not None:
                    return int(v)
        except Exception:
            pass
        return None

    def volume_up(self, step: int = 10):
        if not self.mpv.is_running():
            return
        try:
            cur = self.mpv.get_property("volume", timeout=1.0)
            if cur is None:
                return
            self.set_volume(int(cur) + step)
        except Exception:
            return

    def volume_down(self, step: int = 10):
        if not self.mpv.is_running():
            return
        try:
            cur = self.mpv.get_property("volume", timeout=1.0)
            if cur is None:
                return
            self.set_volume(int(cur) - step)
        except Exception:
            return

    def toggle_mute(self):
        if self.mpv.is_running():
            try:
                cur = self.mpv.get_property("mute")
                self.mpv.set_property("mute", not bool(cur))
            except Exception:
                self.mpv.set_property("mute", True)

    def get_resolution(self) -> str:
        """当前视频分辨率 "WxH"（video-params 实时查）；未播放/未知 → ""（面板状态栏显示）。"""
        try:
            params = self.mpv.get_video_params()
            w = params.get("w")
            h = params.get("h")
            if w and h:
                return f"{w}x{h}"
        except Exception:
            pass
        return ""
