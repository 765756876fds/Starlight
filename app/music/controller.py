"""
音乐控制器 - 高层封装，整合 mpv + 队列 + 数据库 + 搜索

对外提供统一的音乐操作接口。
通过 EventBus 发布音乐事件。
通过 CommandBus 接收音乐命令。
"""
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from ..core.event_bus import Event, EventType, get_event_bus
from .history_recorder import HistoryRecorder
from ..core.command_bus import Command, CommandType, get_command_bus
from ..core.state_store import StateNamespace, get_state_store

from .database import MusicDatabase
from .scanner import MusicScanner
from .mpv import MpvController
from .queue import PlayQueue, PlayMode
from .search import MusicSearch
from .lx_client import LxMusicClient
from .online.events import ONLINE_SEARCH_DONE, ONLINE_ERROR


# mpv http-header-fields 用逗号分隔，值含逗号会破坏语法；只转发安全字段。
_HEADER_WHITELIST = ("Referer", "User-Agent", "Cookie", "Authorization")
_WL_LOWER = {h.lower() for h in _HEADER_WHITELIST}


def _pick_play_headers(headers):
    """过滤插件 headers：只留白名单字段（大小写不敏感，HTTP 头大小写不敏感），
    值含逗号跳过；空返回 None。"""
    if not headers or not isinstance(headers, dict):
        return None
    out = {}
    for k, v in headers.items():
        if k.lower() not in _WL_LOWER:
            continue
        if not isinstance(v, str) or "," in v:
            continue
        out[k] = v
    return out or None


# ---------- 在线选源：同一首歌多源时，音质最优 → 同音质选最快 ----------

def _quality_score(source_name: str, raw) -> int:
    """从插件原始 item 提取音质等级：4=HiRes 3=高品/无损 2=标准 1=低 0=未知。
    字段名来自真实搜索结果核对（kg/kw/wy 用 qualities 档位；溯音用 URL 码率）。"""
    if not isinstance(raw, dict):
        return 0
    src = (source_name or "").lower()
    # 溯音网易：search 直接给 url（share.duanx.cn/.../128k）→ 按 URL 实际码率
    if "溯音" in src:
        m = re.search(r"/(\d+)k", str(raw.get("url") or ""))
        if m:
            b = int(m.group(1))
            return 3 if b >= 320 else (2 if b >= 192 else 1)
        return 0
    # 通用 qualities 档位（kg/kw/wy 真实结构）
    q = raw.get("qualities")
    if isinstance(q, dict):
        keys = set(q.keys())
        if keys & {"super", "lossless", "hires"}: return 4
        if "high" in keys: return 3
        if "standard" in keys: return 2
        if "low" in keys: return 1
        return 0
    # tx 腾讯（当前搜索返回 0 条，留字段兼容）
    if "tx" in src or "腾讯" in src:
        if raw.get("size_flac"): return 3
        if raw.get("size_320mp3"): return 2
        return 0
    return 0


def _normalize(s: str) -> str:
    """归一化：小写 + 去标点空格 + 去 feat. 后缀。归一化相同 = 同一首歌。"""
    s = (s or "").strip().lower()
    s = re.sub(r"[\[\(]feat\..*?[\]\)]", "", s)   # (feat. xx) / [feat. xx]
    s = re.sub(r"\W+", "", s)                      # 去标点/空格/符号
    return s


def _kw_bonus(name: str) -> int:
    """酷我系优先（验证过能播）——同音质同耗时的兜底 tie-break。"""
    n = (name or "").lower()
    return 0 if ("kw" in n or "酷我" in n) else 1


def _pick_candidates(all_tracks, manager):
    """首选组（同一首歌）内按 (音质降序, 耗时升序, 酷我优先, 原顺序) 排序的
    候选列表——调用方逐个试 get_play_url，直到拿到 URL（failover）。
    组间取"首个 source 第一条"所在组（保留原"搜到啥播啥"的相关性语义，
    不全局抢音质）。"""
    if not all_tracks:
        return []
    groups = {}
    for idx, r in enumerate(all_tracks):
        key = _normalize(r.title) + "|" + _normalize(r.artist)
        groups.setdefault(key, []).append((idx, r))
    # 首选组 = 首个 source 第一条所在组（原行为 all_tracks[0] 的歌，相关性最高）
    first_key = _normalize(all_tracks[0].title) + "|" + _normalize(all_tracks[0].artist)
    group = groups[first_key]

    def _lat(name):
        v = manager.get_latency(name) if hasattr(manager, "get_latency") else None
        return v if isinstance(v, (int, float)) else 99.0
    group.sort(key=lambda t: (-_quality_score(t[1].source_name, t[1].extra.get("_raw")),
                              _lat(t[1].source_name),
                              _kw_bonus(t[1].source_name),
                              t[0]))
    return [t[1] for t in group]


def _pick_best(all_tracks, manager):
    """首选（兼容旧接口）：候选列表第一个，无候选返回 None。"""
    cands = _pick_candidates(all_tracks, manager)
    return cands[0] if cands else None


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

    def __init__(self, config: Dict[str, Any]):
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
        self.mpv = MpvController(self.mpv_path, self.mpv_ipc_pipe)

        # 落雪音乐客户端
        self.lx_client = LxMusicClient()
        self.lx_connected = self.lx_client.is_connected()
        if self.lx_connected:
            print('[音乐] 落雪音乐已连接，语音控制将优先控制落雪')
        else:
            print('[音乐] 本地 mpv 播放引擎已就绪')

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
        self._current_song: Optional[Dict[str, Any]] = None
        # P1 候选旁路：最近一次语音搜歌的候选（事件只带展示，track 本体留本对象）
        self._candidate_generation: int = 0
        self._active_candidates: list = []
        # B 方案：换源取消令牌 + 抑制跳歌标记
        self._candidate_switch_token: int = 0
        self._candidate_switching: bool = False

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
        self._duck_original_volume: Optional[int] = None
        self.duck_volume_pct = int(config.get("duck_volume_pct", 50))

        # Highlight Mode 状态
        self._highlight_mode: bool = False
        self._highlight_gen: int = 0  # 每首歌一个 token，旧 poller 失效
        self._highlight_end_time: Optional[float] = None
        self._highlight_song_id: Optional[int] = None
        self._highlight_stop: threading.Event = threading.Event()
        self._highlight_poller: Optional[threading.Thread] = None
        self._skip_pending: bool = False  # 坏文件自动跳过防重入
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
            print(f"[Music] mpv 启动异常: {e!r}")
            ok = False
        if not ok:
            self._mpv_available = False
            print("[Music] mpv 不可用，音乐模块降级运行（播放/音量控制禁用）")
            self._running = True
            self._subscribe_speech_events()
            self.state_store.set(StateNamespace.MUSIC, "volume", self.default_volume)
            self.state_store.set(StateNamespace.MUSIC, "playing", False)
            self.state_store.set(StateNamespace.MUSIC, "current_song", None)
            return False
        self._mpv_available = True
        self.mpv.set_volume(self.default_volume)

        # 注册事件
        self._running = True
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
                except Exception as e:
                    print(f"[Music] idle poll error: {e!r}")
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
                    print("[Music] mpv 进程已退出")
                    self._try_restart_mpv("process-dead")
                    _time.sleep(0.8)
                    continue
                # 3) 原逻辑：idle 上升沿 → 自动切歌
                if now_idle and not self._last_idle:
                    _time.sleep(0.5)  # 等 mpv 稳定
                    print("[Music] 检测到播放结束，自动切下一首")
                    self._auto_next()
                self._last_idle = now_idle
                _time.sleep(0.8)

        threading.Thread(target=_poll_idle, daemon=True, name="music-idle-poll").start()

        # 更新状态
        self.state_store.set(StateNamespace.MUSIC, "volume", self.default_volume)
        self.state_store.set(StateNamespace.MUSIC, "playing", False)
        self.state_store.set(StateNamespace.MUSIC, "current_song", None)

        print("[Music] 模块已启动")
        return True

    def _subscribe_speech_events(self):
        """订阅 TTS 播报事件（播报期间压低音乐音量）。
        在 start() 里订阅：与模块生命周期一致；测试不 start 则不产生全局订阅残留。"""
        self.event_bus.subscribe(EventType.SPEECH_STARTED, self._on_speech_started)
        self.event_bus.subscribe(EventType.SPEECH_FINISHED, self._on_speech_finished)

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
                    print("[Music] mpv 多次重启失败，放弃自动恢复（音乐模块停止自愈）")
                return
            self._mpv_restart_count += 1
            print(f"[Music] mpv 异常({reason})，尝试自动重启 {self._mpv_restart_count}/3")
            self._mpv_restart_pending = True
            restart_song = self._current_song
        threading.Thread(
            target=self._restart_mpv_worker,
            args=(restart_song,),
            daemon=True,
            name="mpv-restart",
        ).start()

    def _restart_mpv_worker(self, restart_song: Optional[Dict[str, Any]]):
        """后台执行实际重启；状态更新后持锁收尾清 pending。"""
        try:
            try:
                self.mpv.stop_process()
            except Exception:
                pass
            try:
                ok = bool(self.mpv.start(taskkill=False, pipe_timeout=3.0, prop_timeout=0.5))
            except Exception as e:
                print(f"[Music] mpv 重启异常: {e!r}")
                ok = False
            if not ok:
                with self._lock:
                    self._mpv_available = False
                print("[Music] mpv 重启失败，模块降级")
                return
            with self._lock:
                self._mpv_available = True
            # 重置轮询基线：mpv 冷启动处于 idle，必须真实读取（失败保守取 True）
            try:
                self._last_idle = bool(self.mpv.get_property("idle-active", timeout=1.0))
            except Exception:
                self._last_idle = True
            # 恢复当前歌曲（仅本地文件；在线 URL 交给 idle 上升沿自动切歌）。
            # 用重启时点快照，避免重启完成时用户已切歌却恢复旧歌。
            self._try_resume_after_restart(restart_song)
        finally:
            with self._lock:
                self._mpv_restart_pending = False

    def _try_resume_after_restart(self, restart_song: Optional[Dict[str, Any]]):
        """重启后恢复播放（用重启时点快照）：本地文件重新 loadfile（从头播）；
        在线 URL 不恢复（源已断），把 _last_idle 置 False 产生上升沿自动切歌。"""
        if not restart_song:
            return
        path = restart_song.get("path") or ""
        if path.startswith(("http://", "https://")):
            self._last_idle = False
            return
        self._play_song(restart_song)

    def stop(self):
        """停止音乐模块"""
        self._running = False
        self._invalidate_highlight()
        if getattr(self, "online_manager", None) is not None:
            try:
                self.online_manager.close()
            except Exception:
                pass
        self.mpv.stop_process()
        print("[Music] 模块已停止")

    def _register_commands(self):
        """注册命令处理器"""
        self.command_bus.register(CommandType.MUSIC_PLAY, lambda c: self.play())
        self.command_bus.register(CommandType.MUSIC_PAUSE, lambda c: self.pause())
        self.command_bus.register(CommandType.MUSIC_RESUME, lambda c: self.resume())
        self.command_bus.register(CommandType.MUSIC_TOGGLE, lambda c: self.toggle())
        self.command_bus.register(CommandType.MUSIC_NEXT, lambda c: self.next())
        self.command_bus.register(CommandType.MUSIC_PREV, lambda c: self.prev())
        self.command_bus.register(CommandType.MUSIC_VOLUME_SET, self._cmd_volume_set)
        self.command_bus.register(CommandType.MUSIC_VOLUME_UP, lambda c: self.volume_up())
        self.command_bus.register(CommandType.MUSIC_VOLUME_DOWN, lambda c: self.volume_down())
        self.command_bus.register(CommandType.MUSIC_SEEK, self._cmd_seek)
        self.command_bus.register(CommandType.MUSIC_SEARCH_PLAY, self._cmd_search_play)
        self.command_bus.register(CommandType.MUSIC_HIGHLIGHT, lambda c: self.jump_to_highlight())
        self.command_bus.register(CommandType.MUSIC_HIGHLIGHT_MODE, self._cmd_highlight_mode)
        self.command_bus.register(CommandType.MUSIC_SET_PLAY_MODE, self._cmd_set_play_mode)
        self.command_bus.register(CommandType.MUSIC_RESCAN, lambda c: self.rescan())
        self.command_bus.register(CommandType.MUSIC_CLEANUP, lambda c: self.cleanup_missing())
        self.command_bus.register(CommandType.MUSIC_CLEAR_QUEUE, lambda c: self.clear_queue())

    # ===== 播放控制 =====

    @_cancel_switch_on_user_action
    def play(self):
        """播放"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
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
                    return
            except Exception as e:
                print(f"[Music] 落雪 play 失败，回落 mpv: {e}")
                self.lx_connected = False
        
        if self.queue.is_empty():
            # 队列空，随机播放
            songs = self.db.get_random_songs(20)
            if songs:
                self.queue.add_all(songs)
                self._play_song(self.queue.next())
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
                    self._play_song(current)
                else:
                    song = self.queue.next()
                    if song:
                        self._play_song(song)
            else:
                self.mpv.play()
                self._update_state(playing=True)
                # 继续播放同一首歌 → 通知其他消费方（电影互斥暂停）；换歌路径由 TRACK_CHANGED 覆盖
                self.event_bus.publish(Event(
                    event_type=EventType.MUSIC_PLAY,
                    data={},
                    source="music",
                ))

    @_cancel_switch_on_user_action
    def pause(self):
        """暂停"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.pause():
                    return
            except Exception as e:
                print(f"[Music] 落雪 pause 失败，回落 mpv: {e}")
                self.lx_connected = False
        
        self.mpv.pause()
        # 实时同步状态
        try:
            self.mpv._properties["pause"] = True
        except Exception:
            pass
        self._update_state(playing=False)

    @_cancel_switch_on_user_action
    def resume(self):
        """继续播放"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        self.mpv.play()
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
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.toggle_play():
                    return
            except Exception as e:
                print(f"[Music] 落雪 toggle 失败，回落 mpv: {e}")
                self.lx_connected = False
        
        self.mpv.toggle_pause()
        # 同步更新状态
        try:
            paused = self.mpv.get_property("pause", timeout=1.0)
            self._update_state(playing=(paused is False))
        except Exception:
            pass

    @_cancel_switch_on_user_action
    def next(self, _timeout: Optional[float] = None):
        """下一首。

        _timeout 非空时：业务锁忙则放弃本轮（自动跳过路径用，防无限等锁）。
        """
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        # 防抖：语音 + UI 并发双触发（<300ms）只跳一首，避免一次跳两首
        now = time.time()
        if now - self._last_next_ts < 0.3:
            print("[Music] next 防抖：300ms 内重复触发已忽略")
            return
        self._last_next_ts = now
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.next():
                    return
            except Exception as e:
                print(f"[Music] 落雪 next 失败，回落 mpv: {e}")
                self.lx_connected = False

        if _timeout is not None:
            if not self._lock.acquire(timeout=_timeout):
                print("[Music] next：业务锁忙，放弃本轮")
                return
            try:
                self._next_locked()
            finally:
                self._lock.release()
            return

        with self._lock:
            self._next_locked()

    def _next_locked(self):
        """next() 的锁内逻辑（持锁调用）。"""
        self._invalidate_highlight()
        song = self.queue.next()
        if song:
            self._play_song(song)
        else:
            # 队列空了，自动加载整个本地库再切
            try:
                all_songs = self.db.get_all_songs(limit=1000)
                if all_songs:
                    self.queue.add_all(all_songs)
                    song = self.queue.next()
            except Exception as e:
                print(f"加载本地队列失败: {e}")
            if not song:
                songs = self.db.get_random_songs(10)
                if songs:
                    self.queue.add_all(songs)
                    song = self.queue.next()
            # 统一出口：无论 song 来自重载全库还是随机兜底，都必须播放
            # （旧代码只在 random 分支内调 _play_song，全库重载路径 song 非空时漏播）
            if song:
                self._play_song(song)

    @_cancel_switch_on_user_action
    def prev(self):
        """上一首"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        # 优先控制落雪
        if self.lx_connected:
            try:
                if self.lx_client.prev():
                    return
            except Exception as e:
                print(f"[Music] 落雪 prev 失败，回落 mpv: {e}")
                self.lx_connected = False
        
        with self._lock:
            self._invalidate_highlight()
            song = self.queue.prev()
            if song:
                self._play_song(song)

    @_cancel_switch_on_user_action
    def play_song(self, song: Dict[str, Any]):
        """播放指定歌曲"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        with self._lock:
            if self._mpv_restart_pending:
                print("[Music] mpv 重启中，忽略播放请求")
                return
            self._invalidate_highlight()
            # 如果队列是空的（语音首次播放本地歌曲），自动把整个本地库加进队列
            if self.queue.size() == 0 and not song.get("online"):
                try:
                    all_songs = self.db.get_all_songs(limit=1000)
                    if all_songs:
                        self.queue.add_all(all_songs)
                except Exception as e:
                    print(f"加载本地队列失败: {e}")
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
            print(f"[Music] get_history error: {e}")
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
            print(f"[Music] enrich cover error: {e}")
        return row

    def lookup_cover(self, row: dict) -> dict:
        """面板侧兜底：返回补全后的行（不修改原字典）。"""
        return self._enrich_cover(dict(row))

    def clear_history(self):
        """清空播放历史。"""
        try:
            self.db.clear_history()
        except Exception as e:
            print(f"[Music] clear_history error: {e}")

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
            print(f"[Music] play_history_item error: {e}")
            return False

    # ---- 歌单 ----

    def create_playlist(self, name: str):
        """新建歌单；重名返回 None。"""
        try:
            return self.db.create_playlist(name)
        except Exception as e:
            print(f"[Music] create_playlist error: {e}")
            return None

    def delete_playlist(self, playlist_id: int):
        """删除歌单（级联删歌单内歌曲）。"""
        try:
            self.db.delete_playlist(playlist_id)
        except Exception as e:
            print(f"[Music] delete_playlist error: {e}")

    def rename_playlist(self, playlist_id: int, name: str) -> bool:
        """重命名歌单；重名返回 False。"""
        try:
            return self.db.rename_playlist(playlist_id, name)
        except Exception as e:
            print(f"[Music] rename_playlist error: {e}")
            return False

    def get_playlists(self) -> list:
        """全部歌单（附 song_count）。"""
        try:
            return self.db.get_playlists()
        except Exception as e:
            print(f"[Music] get_playlists error: {e}")
            return []

    def add_to_playlist(self, playlist_id: int, song: dict) -> bool:
        """加歌到歌单；同歌单内同歌去重（重复返回 False）。"""
        try:
            return self.db.add_to_playlist(playlist_id, song)
        except Exception as e:
            print(f"[Music] add_to_playlist error: {e}")
            return False

    def remove_from_playlist(self, playlist_id: int, song_id: int) -> bool:
        """从歌单移除（song_id = playlist_songs.id）。"""
        try:
            return self.db.remove_from_playlist(playlist_id, song_id)
        except Exception as e:
            print(f"[Music] remove_from_playlist error: {e}")
            return False

    def get_playlist_songs(self, playlist_id: int) -> list:
        """歌单内歌曲（按加入时间）。封面补全：本地反查 path，在线查最近封面缓存。"""
        try:
            rows = self.db.get_playlist_songs(playlist_id)
            return [self._enrich_cover(dict(r)) for r in rows]
        except Exception as e:
            print(f"[Music] get_playlist_songs error: {e}")
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
                    print(f"[Music] 已下载歌优先本地失败: {e}")
            q = " ".join(x for x in (item.get("title"), item.get("artist")) if x).strip()
            if not q:
                return False
            r = self.play_search_result(q, 0)
            return r is not None
        except Exception as e:
            print(f"[Music] play_playlist_item error: {e}")
            return False

    def get_song_path(self, music_id: int) -> str:
        """本地歌磁盘路径（文件不存在返回空串）。"""
        try:
            row = self.db.get_song_by_id(int(music_id))
            if row and row.get("path") and os.path.exists(row["path"]):
                return row["path"]
        except Exception as e:
            print(f"[Music] get_song_path error: {e}")
        return ""

    def add_next(self, song: dict):
        """下一首播放：插到当前播放位置之后（无播放时插队首）。"""
        try:
            pos = self.queue.get_current_index() + 1
            self.queue.add(song, position=pos)
        except Exception as e:
            print(f"[Music] add_next error: {e}")

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
            print(f"[Music] delete_local_file error: {e}")
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

    @_cancel_switch_on_user_action
    def play_search_result(self, query: str, index: int = 0):
        """搜索并播放第 N 个结果，本地找不到自动走在线fallback"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return None
        results = self.search.search(query, limit=10)
        if results and 0 <= index < len(results):
            self.play_song(results[index])
            return results[index]

        # 本地搜索失败，尝试在线fallback
        if self.online_manager is not None:
            _t_online_start = time.time()
            try:
                print(f"[Music] 本地未找到'{query}'，尝试在线搜索...")
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
                        print(f"[Music] 候选推送失败（不影响播放）: {_e}")
                    _t_url_start = time.time()
                    chosen, play_result = None, None
                    for cand in candidates:
                        play_result = self.online_manager.get_play_url(cand)
                        if play_result and play_result.get("url"):
                            chosen = cand
                            break

                    if chosen is None:
                        print(f"[Music] 获取播放URL失败（{len(candidates)} 个候选均无 URL），无法在线播放")
                        return None

                    print(f"[Music] 在线找到: {chosen.title} - {chosen.artist} (音源: {chosen.source_name})")
                    # 定向取 URL：只调 chosen 对应的音源（manager 按 track.source_name 路由）。
                    # 旧逻辑遍历所有源串行调 get_play_url，demo/archive 对陌生 track 各自
                    # 发起 HTTP（超时 8s/个），导致在线播放最高 ~16s 延迟。
                    _t_url_ms = int((time.time() - _t_url_start) * 1000)
                    print(f"[Music] 获取播放URL，耗时 {_t_url_ms}ms")
                    if play_result:
                        print(f"[Music] 获取播放URL成功，开始在线播放...")
                        play_url = play_result.get("url")
                        # 在线URL需要HTTP头。mpv 的 http-header-fields 是持久属性，
                        # 某源 Referer 一旦设置会跨源残留。因此每次播放前无条件重设
                        # （覆盖式 set_property）：插件 headers（白名单）优先，
                        # 否则仅酷我系源带 Referer，其他源重置为纯 UA。
                        hdr = _pick_play_headers(play_result.get("headers"))
                        need_kw_header = ("kw" in chosen.source_name.lower()
                                          or "酷我" in chosen.source_name
                                          or "kuwo" in (play_url or "").lower())   # URL 是酷我 CDN → 带酷我 Referer
                        try:
                            # 双保险：先清空 http-header-fields，再按分支设置——
                            # 无论 mpv 对 list 属性是"覆盖"还是"追加"，旧 headers 都不会残留。
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
                        self.mpv.loadfile(play_url)
                        self.mpv.set_property("pause", False)
                        try:
                            self.mpv._properties["pause"] = False
                        except Exception:
                            pass
                        online_song = {
                            "id": -1,
                            "title": chosen.title,
                            "artist": chosen.artist,
                            "album": chosen.album,
                            "duration": getattr(chosen, 'duration_seconds', 0) or 0,
                            "path": play_url,
                            "source": "online",
                            "source_name": chosen.source_name,
                            "track_id": chosen.track_id,
                            "extra": getattr(chosen, 'extra', None) or {},
                        }
                        self._current_song = online_song
                        self._update_state(
                            playing=True,
                            current_song={
                                "id": -1,
                                "title": chosen.title,
                                "artist": chosen.artist,
                                "album": chosen.album,
                                "duration": getattr(chosen, 'duration_seconds', 0) or 0,
                                "path": play_url,
                            }
                        )
                        self.event_bus.publish(Event(
                            event_type=EventType.MUSIC_TRACK_CHANGED,
                            data={"song": online_song},
                            source="music"
                        ))
                        _t_total_ms = int((time.time() - _t_online_start) * 1000)
                        print(f"[Music] 在线播放就绪，总耗时 {_t_total_ms}ms（搜索{_t_search_ms}ms + URL{_t_url_ms}ms）")
                        return online_song
                    else:
                        # 定向/兜底取 URL 均失败：明确提示，避免静默不播放
                        print(f"[Music] 获取播放URL失败（音源: {chosen.source_name}），无法在线播放")
            except Exception as e:
                print(f"[Music] 在线搜索失败: {e}")

        return None

    def _play_online_queue_item(self, song: Dict[str, Any]):
        """队列在线歌播放：按 title+artist 重搜（不用存的 track_id——旧 track 会失效，
        日志 raw=None 证实），候选逐个取 URL → loadfile。失败 → 延迟 next() 主动跳过：
        轮询上升沿会被 _last_idle=now_idle 覆盖吞掉（_play_song 的 reset 在 auto_next 同步
        返回后被 L295 覆盖），必须主动跳，不能等轮询。不发 MUSIC_TRACK_CHANGED
        （rule_engine/speech_judge 语音订阅者），直接 record_song 记历史。"""
        query = f"{song.get('title', '')} {song.get('artist', '')}".strip()
        if not query:
            print("[Music] 队列在线歌无 title，跳过")
            self._schedule_skip_online(song)
            return
        try:
            online_dict = self.online_manager.search_all(query, limit_per_source=5)
            all_tracks = [t for tracks in online_dict.values() for t in tracks]
            if not all_tracks:
                print("[Music] 队列在线歌重搜无结果，跳过")
                self._schedule_skip_online(song)
                return
            candidates = _pick_candidates(all_tracks, self.online_manager)
            chosen, play_result = None, None
            for cand in candidates:
                play_result = self.online_manager.get_play_url(cand)
                if play_result and play_result.get("url"):
                    chosen = cand
                    break
            if chosen is None:
                print("[Music] 队列在线歌重搜 URL 均失败，跳过")
                self._schedule_skip_online(song)
                return
            url = play_result["url"]
            print(f"[Music] 队列在线歌重搜播放: {chosen.title} - {chosen.artist} ({chosen.source_name})")
            # http-header-fields 持久属性：每次无条件重设，避免 Referer 跨源残留
            hdr = _pick_play_headers(play_result.get("headers"))
            need_kw_header = ("kw" in chosen.source_name.lower()
                              or "酷我" in chosen.source_name
                              or "kuwo" in (url or "").lower())
            try:
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
            self.mpv.loadfile(url)
            self.mpv.set_property("pause", False)
            try:
                self.mpv._properties["pause"] = False
            except Exception:
                pass
            online_song = {
                "id": -1,
                "title": chosen.title,
                "artist": chosen.artist,
                "album": chosen.album,
                "duration": getattr(chosen, 'duration_seconds', 0) or 0,
                "path": url,
                "source": "online",
                "source_name": chosen.source_name,
                "track_id": chosen.track_id,
                "extra": getattr(chosen, 'extra', None) or {},
            }
            self._current_song = online_song
            self._update_state(playing=True, current_song=online_song)
            try:
                self.history_recorder.record_song({
                    "id": -1,
                    "title": chosen.title,
                    "artist": chosen.artist,
                    "source": "online",
                    "source_name": chosen.source_name,
                    "track_id": chosen.track_id,
                })
            except Exception as e:
                print(f"[Music] 记录播放历史失败: {e}")
        except Exception as e:
            print(f"[Music] 队列在线歌播放失败: {e}")
            self._schedule_skip_online(song)

    def _schedule_skip_online(self, song: Dict[str, Any]):
        """延迟 next() 跳过队列在线歌（不依赖轮询上升沿——会被 _last_idle 覆盖吞掉）。"""
        threading.Timer(0.2, lambda: self._do_skip_online(song)).start()

    def _do_skip_online(self, song: Dict[str, Any]):
        """跳队列在线歌：队列 current 仍是该 song 才跳（防覆盖用户切歌操作）。"""
        try:
            if self.queue.get_current() is not song:
                return
            self.next(_timeout=0.5)
        except Exception as e:
            print(f"[Music] 跳过队列在线歌 next() 异常: {e!r}")

    def _play_song(self, song: Dict[str, Any]):
        """实际播放歌曲"""
        if not song:
            return

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
            print(f"[Music] 文件不存在: {filepath}")
            self._missing_file_streak += 1
            if self._missing_file_streak >= 3:
                # 限次保护：连续 3 次文件不可用 → 放弃播放，避免无限空转
                self._missing_file_streak = 0
                print("[Music] 连续 3 次文件不可用，停止播放")
                self.stop()
                return
            # 单曲循环 + 文件不存在 → 跳出循环，顺序切下一首（防卡死）
            if self.queue.get_play_mode() == PlayMode.REPEAT_ONE:
                print("[Music] 单曲循环文件不存在，顺序切下一首")
                self.queue.skip_current()
            # 防御性明确：本分支 return 前重置轮询状态（L673 已置过，
            # 保留以防御未来该行被移动后回归）
            self._last_idle = False
            return

        self.mpv.loadfile(filepath)
        # 播放成功 → 重置连续失败计数
        self._missing_file_streak = 0
        # 确保新歌曲开始播放（不继承之前的暂停状态）
        self.mpv.set_property("pause", False)
        try:
            self.mpv._properties["pause"] = False
        except Exception:
            pass
        self._current_song = song

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

    def _confirm_playback(self, song: Dict[str, Any], filepath: str):
        """loadfile 后异步确认播放是否真正开始。
        判定失败（mpv 仍 idle / 拿不到播放时间）→ 自动切下一首跳过坏文件。"""
        import time as _t
        _t.sleep(0.8)
        try:
            idle_now = bool(self.mpv.get_property("idle-active", timeout=0.5))
            pt = self.mpv.get_property("playback-time", timeout=0.5)
            dur = self.mpv.get_property("duration", timeout=0.5)
        except Exception:
            idle_now, pt, dur = True, None, None
        # 期间用户已切走/正常播完 → 不干预
        if self._current_song is not song:
            return
        bad = idle_now or (pt is None and dur is None)
        if bad:
            # 坏文件自动跳过静默执行（不再打印到控制台）
            self._schedule_skip_bad(song)

    def _schedule_skip_bad(self, song: Dict[str, Any]):
        """延后执行 next() 跳过坏文件（防重入 + 防覆盖用户操作）"""
        if self._current_song is not song or self._skip_pending:
            return
        self._skip_pending = True
        threading.Timer(0.2, lambda: self._do_skip_bad(song)).start()

    def _do_skip_bad(self, song: Dict[str, Any]):
        self._skip_pending = False
        if self._current_song is not song:
            return
        try:
            # 抢锁 0.5s：锁忙（UI/重启持锁）则放弃本轮，下轮 confirm/轮询再处理
            self.next(_timeout=0.5)
        except Exception as e:
            print(f"[Music] 跳过坏文件时 next() 异常: {e!r}")

    # ===== 音量控制 =====

    def set_online_manager(self, manager) -> None:
        self.online_manager = manager
        # 下载器随在线源就绪创建（不阻塞 UI、独立线程池，不碰本对象 mpv）
        if self.downloader is None:
            try:
                from app.music.downloader import DownloadManager
                self.downloader = DownloadManager(
                    manager, self.db, self.download_dir, max_workers=3)
            except Exception as e:
                print(f"[Music] 下载器初始化失败: {e}")

    def download_song(self, song: Dict[str, Any], callbacks=None) -> bool:
        """在线歌下载入队（薄代理）。callbacks: on_progress/on_done/on_fail/on_exists。"""
        if self.downloader is None:
            if callbacks and callbacks.get("on_fail"):
                key = f"{song.get('title', '')}|{song.get('artist', '')}".strip('|')
                callbacks["on_fail"](key, "下载服务未就绪")
            return False
        try:
            return self.downloader.download(song, callbacks)
        except Exception as e:
            print(f"[Music] download_song error: {e}")
            return False

    def is_downloaded(self, title: str, artist: str) -> bool:
        """下载目录里已有 歌名 - 歌手.* → 已下载。"""
        if self.downloader is None:
            return False
        try:
            return self.downloader.is_downloaded(title, artist)
        except Exception:
            return False

    def remove_download(self, song: Dict[str, Any]) -> tuple:
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
                print(f"[Music] 移除下载记录失败: {e}")
        return ok, path, err

    def sync_custom_sources(self) -> int:
        """把 config/music_sources.json 中启用的自定义音源同步注册进 online_manager。

        幂等：先注销上次同步注册的 HTTP 源，再按当前启用列表重建。
        - 跳过 api_url 为空的源（如仅存元信息的 JS 导入）
        - 不覆盖 js_bridge / demo / archive / Python 插件等内置源（重名保护）
        返回本次注册成功的源数量。任何源失效只影响自身，不影响其他源。"""
        if self.online_manager is None:
            return 0
        from app.music.source_manager import MusicSourceManager
        from app.music.online.sources.http_source import HttpJsonSource

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
                print(f"[Music] 自定义源 '{s.name}' 为 js 元信息，尝试按 HTTP JSON 协议注册"
                      f"（若失效请在 UI 中改用 JSON 导入）")
            # 重名保护：name 已被内置/插件源占用时跳过，避免覆盖 js_bridge 等
            if s.name in self.online_manager._sources:
                print(f"[Music] 自定义源 '{s.name}' 与已注册音源重名，跳过")
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
            print(f"[Music] 自定义音源已同步: {sorted(self._custom_http_sources)}")
        return registered

    def search_online(self, keyword: str) -> dict:
        if self.online_manager is None:
            return {}
        return self.online_manager.search_all(keyword)

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
            print(f"[Music] get_top_lists fail: {e}")
            return {}

    def get_top_list_detail(self, source: str, list_id: str, page: int = 1):
        """单榜单详情：{is_end, tracks: [OnlineTrack]}。失败 → None。"""
        if self.online_manager is None:
            return None
        try:
            return self.online_manager.get_top_list_detail(source, list_id, page)
        except Exception as e:
            print(f"[Music] get_top_list_detail fail: {e}")
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
            print(f"[Music] search_local error: {e}")
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
                            print("[Music] 换源被用户操作取消")
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
                                print(f"[Music] 换源成功: {src}")
                                return
                    if my_token == self._candidate_switch_token:
                        print("[Music] 该歌所有音源均无法播放")
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
            print(f"[Music] play_online_resume error: {e}")
            return False

    def play_online(self, track, _before_load=None) -> bool:
        """通过 online source 获取 URL 后用 mpv 直接播放。"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return False
        if self.online_manager is None:
            return False
        result = self.online_manager.get_play_url(track)
        if not result:
            print(f"[Music] online 无 URL: {track.title}")
            return False
        url = result.get("url")
        print(f"[Music] play online: {track.title} -> {url[:80]}")
        try:
            # mpv 的 http-header-fields 是持久属性：每次播放前无条件重设，
            # 避免某源 Referer 跨源残留（覆盖式 set_property）。
            # 优先级：插件返回 headers（白名单过滤） > source_name 硬编码（酷我）。
            # 设置失败仅跳过 header，不影响播放流程。
            hdr = _pick_play_headers(result.get("headers"))
            try:
                # 双保险：先清空 http-header-fields，再按分支设置——
                # 无论 mpv 对 list 属性是"覆盖"还是"追加"，旧 headers 都不会残留。
                self.mpv.set_property("http-header-fields", "")
                if hdr:
                    fields = ",".join(f"{k}: {v}" for k, v in hdr.items())
                    self.mpv.set_property("http-header-fields", fields)
                elif ("kw" in (track.source_name or "").lower()
                      or "酷我" in (track.source_name or "")):
                    self.mpv.set_property(
                        "http-header-fields",
                        "Referer: https://www.kuwo.cn/,User-Agent: Mozilla/5.0")
                else:
                    self.mpv.set_property(
                        "http-header-fields",
                        "User-Agent: Mozilla/5.0")
            except Exception:
                pass
            # 校验必须紧贴 loadfile：此前不得有释放 GIL 的操作，
            # 确保"校验→loadfile"原子（用户取消后不会后发 loadfile 劫持播放）。
            if _before_load is not None and not _before_load():
                return False
            self.mpv.loadfile(url)
            print("[Music] mpv loadfile ok")
        except Exception as e:
            print(f"[Music] mpv loadfile fail: {e}")
            return False
        self.remember_cover(
            title=track.title or "",
            artist=track.artist or "",
            source_name=getattr(track, "source_name", "") or "",
            track_id=getattr(track, "track_id", "") or "",
            cover_url=getattr(track, "cover_url", None) or "")
        self._current_song = {
            "id": None,
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "duration": track.duration_seconds,
            "path": url,
            "source": "online",
            "source_name": getattr(track, "source_name", "") or "",
            "track_id": getattr(track, "track_id", "") or "",
            "extra": getattr(track, "extra", None) or {},
        }
        self._update_state(
            playing=True,
            current_song={
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "duration": track.duration_seconds,
                "path": url,
                "online": True,
            }
        )
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
            print(f"[Music] 记录播放历史失败: {e}")
        return True

    def set_volume(self, volume: int):
        """设置音量"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        volume = max(0, min(100, volume))
        self.mpv.set_volume(volume)
        self.state_store.set(StateNamespace.MUSIC, "volume", volume)

    def volume_up(self, step: int = 5):
        """音量增加"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        current = self.mpv.get_volume()
        self.set_volume(current + step)

    def volume_down(self, step: int = 5):
        """音量减少"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        current = self.mpv.get_volume()
        self.set_volume(current - step)

    def get_volume(self) -> int:
        """获取当前音量"""
        return self.mpv.get_volume()

    # ===== 跳转 =====

    @_cancel_switch_on_user_action
    def seek(self, seconds: float, mode: str = "relative"):
        """跳转"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
            return
        self.mpv.seek(seconds, mode)

    @_cancel_switch_on_user_action
    def jump_to_highlight(self):
        """跳到高潮部分（一次性）"""
        if not self._mpv_available:
            print("[Music] mpv 不可用，忽略操作")
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
                print(f"[Music] highlight seek 失败: {e}")
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
                    print(f"[Music] highlight fallback seek 失败: {e}")

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
        """用户手动标记当前播放位置为高潮起点（+35 秒段）"""
        song = self.get_current_song()
        if not song:
            return False
        pos = self.mpv.get_live_playback_time()
        if not pos or pos < 5:
            return False
        end = min(pos + 35.0, self.mpv.get_duration() or pos + 35.0)
        self.db.save_highlight(song["id"], pos, end, confidence=1.0, source="manual")
        print(f"[Music] manual highlight {song.get('title')}: {pos:.1f}-{end:.1f}s")
        return True

    def get_play_mode(self) -> str:
        return self.queue.get_play_mode()

    def _start_highlight_segment(self, song: Dict[str, Any]):
        """为当前歌曲启动 highlight 播放段"""
        song_id = song.get("id")
        highlight = self.db.get_highlight(song_id)
        if not highlight:
            # 无 highlight → 整首正常播放，不 fallback
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
                print(f"[Music] highlight seek to {start_sec:.1f}s")
            except Exception as e:
                print(f"[Music] highlight seek failed: {e}")

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

    def rescan(self, full: bool = False) -> Dict[str, int]:
        """重新扫描音乐库"""
        print(f"[Music] 开始扫描音乐库 (full={full})...")
        result = self.scanner.scan(full_rescan=full)
        print(f"[Music] 扫描完成: {result}")
        self.state_store.set(StateNamespace.MUSIC, "song_count", self.db.get_song_count())
        # 扫描完成后后台计算highlight，不阻塞
        self.scanner.start_highlight_background()
        return result

    def cleanup_missing(self) -> Dict[str, int]:
        """清理文件已不存在的歌曲记录（含其 highlight）"""
        result = self.db.delete_missing_songs()
        print(f"[Music] 清理失效歌曲: {result}")
        self.state_store.set(StateNamespace.MUSIC, "song_count", self.db.get_song_count())
        return result

    def get_song_count(self) -> int:
        """获取歌曲总数"""
        return self.db.get_song_count()

    # ===== 队列管理 =====

    def get_queue(self) -> List[Dict[str, Any]]:
        """获取播放队列"""
        return self.queue.get_queue()

    @_cancel_switch_on_user_action
    def clear_queue(self):
        """清空队列：同时停止当前播放、失效 highlight、重置轮询基线。
        旧行为只清队列数据 → 播完自动重载全库续播（清空无意义 + 状态错乱）。"""
        with self._lock:
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

    def set_play_mode(self, mode: str):
        """设置播放模式（顺序/随机/单曲/列表循环）+ 写回 settings.yaml music.play_mode。
        延迟 import：app/music 不依赖 app/ui 的模块级导入（防环）。"""
        self.queue.set_play_mode(mode)
        try:
            from app.ui.settings_store import save_settings_yaml
            save_settings_yaml({"music": {"play_mode": mode}})
        except Exception as e:
            print(f"[Music] 播放模式写回配置失败: {e!r}")

    def get_current_song(self) -> Optional[Dict[str, Any]]:
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
        self.set_volume(int(volume))

    def _cmd_seek(self, command: Command):
        seconds = command.get("seconds", 0)
        mode = command.get("mode", "relative")
        self.seek(float(seconds), mode)

    def _cmd_search_play(self, command: Command):
        query = command.get("query", "")
        index = command.get("index", 0)
        self.play_search_result(query, int(index))

    def _cmd_highlight_mode(self, command: Command):
        enabled = command.get("enabled", True)
        self.set_highlight_mode(bool(enabled))

    def _cmd_set_play_mode(self, command: Command):
        mode = command.get("mode", "sequential")
        self.set_play_mode(mode)

    # ===== 事件处理 =====

    def _on_idle_change(self, value: Any):
        """mpv 空闲状态变化"""
        if value and self._running:
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
        if self._candidate_switching:
            print("[Music] 换源中，跳过自动切歌")
            return
        if not self._running:
            return
        # 守卫必须与 _poll_idle 同源（实时 get_property）。
        # 不能用 self.mpv.is_idle()：它读的是启动时初始化的属性缓存，
        # mpv 事件回调在 _send_command 中被丢弃，idle-active 缓存从不更新，
        # 初始读失败时默认 False → 会拦截已被实时确认的切歌（next() 永不执行）。
        try:
            idle_now = bool(self.mpv.get_property("idle-active", timeout=0.5))
        except Exception as e:
            print(f"[Music] auto_next idle check error: {e!r}")
            return
        if not idle_now:
            print("[Music] auto_next: idle-active 实时为 False，跳过切歌")
            return
        print("[Music] auto_next: 已确认 idle，执行 next()")
        try:
            self.next()
            print("[Music] auto_next: next() 完成")
        except Exception as e:
            print(f"[Music] auto_next: next() 异常: {e!r}")

    def _update_state(self, **kwargs):
        """更新状态存储"""
        for key, value in kwargs.items():
            self.state_store.set(StateNamespace.MUSIC, key, value)

    def is_playing(self) -> bool:
        """是否正在播放"""
        if not self._mpv_available:
            return False
        return not self.mpv.is_paused() and not self.mpv.is_idle()








