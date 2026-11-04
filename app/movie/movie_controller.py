"""
电影链控制器 - 苹果 CMS 搜索 + mpv 独立视频实例播放。

- 与音乐链互斥：播电影前停音乐（直接调 music.stop()）；音乐开始播新歌时
  电影订阅 MUSIC_TRACK_CHANGED 事件自行停止（事件解耦，音乐链零改动）。
- mpv 独立实例（video=True 出视频窗口，taskkill=False 不杀音乐实例）。
- 剧集选集 + 自动连播（idle 上升沿检测）+ Assrt 字幕（搜索/下载/解压/加载）。
"""
import json
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

import requests

from ..core.config import get_config, resolve_mpv_path
from ..core.event_bus import EventType, get_event_bus
from ..core.logger import get_logger
from ..core.path_resolve import PROJECT_ROOT
from ..core.state_store import StateNamespace, get_state_store
from ..music.mpv import MpvController
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


def parse_cms_response(text):
    """苹果 CMS 响应解析。

    已知坑：接口 Content-Type 常标 text/html 但 body 实际是合法 JSON
    （如 360zyzz 的 /api.php/provide/vod/...）。因此解析必须按 body 首字符
    判断（{ 或 [），不能按 Content-Type。非 JSON body 返回 {}。
    """
    s = (text or "").lstrip()
    if not s or s[0] not in "{[":
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def split_play_url(vod_play_url):
    """拆分苹果 CMS 的 vod_play_url。

    格式：名字$URL#名字2$URL2（# 分集、$ 分名与 URL）。
    返回 [(name, url), ...]；无有效段时返回 []。
    """
    out = []
    for seg in str(vod_play_url or "").split("#"):
        seg = seg.strip()
        if not seg or "$" not in seg:
            continue
        name, _, url = seg.partition("$")
        if not url.strip():
            continue
        out.append((name.strip(), url.strip()))
    return out


def first_play_url(vod_play_url):
    """取第一条 (name, url)；无则 None。"""
    parts = split_play_url(vod_play_url)
    return parts[0] if parts else None


def episode_eps(item):
    """取 item 的分集列表（非 dict / 无 play_url → 空列表）——episodes
    与 is_multi_episode 共用，消重复取数逻辑。"""
    if not isinstance(item, dict):
        return []
    return split_play_url(item.get("play_url") or "")


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
    return args


class MovieController:
    """搜索 + 播放控制 + 与音乐互斥（事件解耦）。"""

    def __init__(self, source_url=None, mpv_path=None, ipc_pipe=None,
                 music=None, config=None, source_manager=None,
                 voice_feedback=None, speech_queue=None):
        self._config = config or get_config()
        # P1：语音搜电影结果 → UI 反馈回调（MoviePanel 注入；None 时只 print 兜底）
        self._voice_feedback = voice_feedback
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
            bus.register(CommandType.MOVIE_PAUSE, lambda c: self.pause())
            bus.register(CommandType.MOVIE_RESUME, lambda c: self.resume())
            bus.register(CommandType.MOVIE_NEXT_EPISODE, lambda c: self.next_episode())
            bus.register(CommandType.MOVIE_PREV_EPISODE, lambda c: self.prev_episode())
            bus.register(CommandType.MOVIE_STOP, lambda c: self.stop())
            bus.register(CommandType.MOVIE_VOLUME_SET, lambda c: self.set_volume(c.get("volume", 50)))
            bus.register(CommandType.MOVIE_VOLUME_UP, lambda c: self.volume_up(c.get("step", 5)))
            bus.register(CommandType.MOVIE_VOLUME_DOWN, lambda c: self.volume_down(c.get("step", 5)))
            bus.register(CommandType.MOVIE_SEEK, lambda c: self.seek(c.get("seconds", 0), c.get("mode", "relative")))
        except Exception as e:
            logger.error(f"[Movie] 注册语音命令失败: {e}")

    def _cmd_search_play(self, command):
        """语音"播放电影XX"：搜索 → 播 Top1（语音直达，不弹 UI 选集对话框）。
        包2c：params["episode"] 直跳对应集（"播放电影甄嬛传第5集"→第5集）；
        无集数 → 播第一集（现状不变）；集数越界 → 提示 + False（不崩）。"""
        def current():
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
            ok = self.play(item) if index is None else self.play(item, index=index)
            if ok is False:
                notify("电影加载未成功，请尝试其他片源")
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

    def search(self, keyword, timeout=10.0):
        """苹果 CMS 多源搜索（策略 C：当前源挂了自动换，最多试 3 个启用源）。

        返回 [{vod_id, vod_name, vod_remarks, play_url, first_name, first_url, _source}]；
        命中 → last_search_source=源名；全部失败 → [] + last_search_error（面板提示）。
        不自动改 active_source（本次临时换，用户选的源保留）。"""
        if not keyword or not keyword.strip():
            return []
        sources = self._source_manager.get_enabled_sources()
        if not sources:
            self.last_search_error = "无可用源"
            return []
        active = self._source_manager.get_active_name()
        # 当前源优先，其余按序（最多试 3 个）
        ordered = ([s for s in sources if s.get("name") == active]
                   + [s for s in sources if s.get("name") != active])[:3]
        errors = []
        for src in ordered:
            try:
                items = self._search_one(src["url"], keyword, timeout)
            except Exception as e:
                errors.append(f"{src['name']}: {type(e).__name__}")
                continue
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
                self.last_search_source = src["name"]
                self.last_search_error = ""
                return items
            errors.append(f"{src['name']}: 无结果")
        self.last_search_source = None
        self.last_search_error = "；".join(errors) or "无可用源"
        return []

    def _search_one(self, url, keyword, timeout):
        """单源搜索（策略 C 的单个源执行）。网络失败抛异常（上层换下一个源）。"""
        params = {"ac": "videolist", "wd": keyword.strip()}
        r = requests.get(url, params=params, timeout=timeout, headers=UA)
        data = parse_cms_response(r.text)
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

    def play(self, item, index=None):
        """播电影：先停音乐（互斥），再起视频 mpv 播指定集（默认第一条 URL）。
        index=None → 取 first_url（现状不变）；index=N → 取第 N 集 URL。失败返回 False。
        内嵌：embed 开且有播放窗口注入 winId → mpv --wid 渲染进 Qt 容器；
        否则（embed 关 / 窗口未建）→ 独立 mpv 窗口（现状，安全回退）。"""
        if not isinstance(item, dict):
            return False
        url = item.get("first_url") or ""
        idx = 0
        eps = self.episodes(item)
        if index is not None:
            idx = index
            if 0 <= idx < len(eps):
                url = eps[idx][1]
            else:
                print(f"[Movie] 集数越界: index={index} 总集数={len(eps)}")
                return False
        elif eps:
            idx = 0
            url = eps[0][1]
        if not url:
            print(f"[Movie] 无可用播放地址: {item.get('vod_name')}")
            return False
        # 互斥 1：电影 → 音乐（暂停，保留队列与进度；music.pause() 不发事件，无循环）
        music = self._music
        if music is not None:
            try:
                music.pause()
            except Exception as e:
                logger.error(f"[Movie] 暂停音乐失败: {e}")
        # 内嵌 wid：embed 开且窗口已注入 → 传 --wid；否则 None（独立窗口，现状）
        wid = self._embed_wid if (self._embed and self._embed_wid) else None
        if wid is not None:
            print(f"[Movie] 内嵌播放 wid={wid}")
        # 独立视频实例：taskkill=False 不杀音乐实例；video=True 出视频窗口
        if not self.mpv.is_running():
            if not self.mpv.start(taskkill=False, wid=wid):
                print("[Movie] mpv 启动失败")
                return False
        # loadfile 三态确认（与 mpv.stop() 同契约；命令被接受≠媒体已解码/出声）：
        #   resp.error == "success" → 命令被接受，按原逻辑更新播放上下文
        #   resp is None（超时/无响应）→ 结果未确认 → 不伪造播放成功
        #   resp.error != "success" → 协议层明确失败 → 不伪造播放成功
        #   抛异常（IPC 未连接/写入失败）→ 不伪造播放成功。
        # 失败时音乐已按互斥先行暂停（既有行为，如实保留，不加回滚）。
        try:
            resp = self.mpv.loadfile(url)
        except Exception as e:
            logger.error(f"[Movie] loadfile 异常: {e!r}")
            return False
        if resp is None:
            logger.error("[Movie] loadfile 结果未确认（IPC 超时/无响应）")
            return False
        if not isinstance(resp, dict) or resp.get("error") != "success":
            err = resp.get("error") if isinstance(resp, dict) else resp
            logger.error(f"[Movie] loadfile 失败: error={err}")
            return False
        # 确保新视频开始播放（不继承之前的暂停状态）
        self.mpv.set_property("pause", False)
        try:
            self.mpv._properties["pause"] = False
        except Exception:
            pass
        self._current = item
        self._current_index = idx
        self._playback_gen += 1  # V2-2：新片加载，会话代次递增
        self._last_idle = False  # 新播放开始，等播完再触发连播
        self._external_url_mode = False  # 切回正常剧集模式
        self._external_url_seen_active = False
        self._pending_history = (dict(item), str(url))
        self._history_recorded_gen = None
        self._write_state(playing=True, paused=False)
        self._pending_local_subtitle = None
        if item.get("local_file"):
            self._load_local_subtitle(str(url))
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

    def play_external_url(self, url: str, title: str = "外部流") -> bool:
        """播放任意 URL（解析出来的外链，无 vod_id/剧集结构）。

        与 play() 同语义：先 pause 音乐互斥，启动 mpv，loadfile。
        不写 _current/_current_index（不是资源站条目，无连播/切集）。
        失败返回 False；成功返回 True。
        """
        if not url:
            return False
        music = self._music
        if music is not None:
            try:
                music.pause()
            except Exception as e:
                logger.error(f"[Movie] 暂停音乐失败: {e}")
        wid = self._embed_wid if (self._embed and self._embed_wid) else None
        if not self.mpv.is_running():
            if not self.mpv.start(taskkill=False, wid=wid):
                print("[Movie] mpv 启动失败（外部 URL）")
                return False
        try:
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
        self._external_url_seen_active = False  # 缓冲期保护
        self._write_state(playing=True, paused=False)
        return True

    # ---- 自动连播 ----

    def set_auto_next(self, enabled: bool):
        """连播开关（面板反映；关闭后播完不再切下一集）。"""
        self._auto_next = bool(enabled)

    def is_auto_next(self) -> bool:
        return self._auto_next

    def tick(self):
        """供面板轮询调用（500ms）：检测 mpv idle 上升沿 → 播完自动下一集。
        手动 stop（_current=None）/ 开关关 → 不连播；末集播完 → 停（不循环）。"""
        self._apply_pending_local_subtitle()
        # 外部 URL 模式：播完 idle 上升沿 → 写 playing=False，不连播。
        # 缓冲期保护：loadfile 成功 ≠ 媒体已播放，先观察到 idle=False 才认可"正在播"。
        if getattr(self, "_external_url_mode", False):
            now_idle = self.mpv.get_idle_active()
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
        if self._current is None or not self._auto_next:
            self._last_idle = self.mpv.get_idle_active()
            return
        if not self.mpv.is_running():
            return
        now_idle = self.mpv.get_idle_active()
        self._record_history_if_started(now_idle)
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
        return True

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
            resp = self.mpv.loadfile(url)
        except Exception as e:
            logger.error(f"[Movie] 切上一集 loadfile 异常: {e!r}")
            return False
        if resp is None:
            return False
        if not isinstance(resp, dict) or resp.get("error") != "success":
            return False
        self._current_index = prev
        logger.info(f"[Movie] 切到上一集: {name}")
        return True

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
        self._stopping = True
        try:
            if self.mpv.is_running():
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
            pos = self.get_position()      # 进程死 → 回退最后成功轮询缓存（≤500ms 滞后）
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
        self._last_idle = False      # 恢复播放：重置连播基线（防误判 idle 上升沿）
        self._write_state(playing=True, paused=False)
        print("[Movie] mpv 崩溃恢复完成（wid 重传 + loadfile 续播）")

    # ---- 播放控制（复用 mpv JSON IPC） ----

    def pause(self):
        """暂停电影（保留进度；_pausing 守卫防互斥循环）。"""
        if self._pausing:
            return
        self._pausing = True
        try:
            if self._current is not None and self.mpv.is_running():
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

    def resume(self):
        if self._current is None or not self.mpv.is_running():
            return False
        self.mpv.play()
        if self.mpv.get_property("pause", timeout=0.5) is not False:
            return None
        if self._current is not None:
            self._write_state(playing=True, paused=False)
            self._notify_voice("电影继续播放")
        return True

    def seek(self, seconds, mode="relative"):
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
        if self._subscribed:
            try:
                bus = get_event_bus()
                bus.unsubscribe(EventType.MUSIC_TRACK_CHANGED, self._on_music_playing)
                bus.unsubscribe(EventType.MUSIC_PLAY, self._on_music_playing)
                bus.unsubscribe(EventType.SYSTEM_SHUTDOWN, self._on_shutdown)
            except Exception:
                pass
            self._subscribed = False
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
