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
import zipfile

import requests

from ..core.config import get_config, resolve_mpv_path
from ..core.event_bus import EventType, get_event_bus
from ..core.logger import get_logger
from ..core.path_resolve import PROJECT_ROOT
from ..core.state_store import StateNamespace, get_state_store
from ..music.mpv import MpvController
from .source_manager import MovieSourceManager, _SingleSourceManager

logger = get_logger()

# 默认源：360资源（苹果 CMS 标准接口）
DEFAULT_SOURCE_URL = "https://360zyzz.com/api.php/provide/vod/from/360m3u8/at/json"
DEFAULT_IPC_PIPE = r"\\.\pipe\lol_movie_mpv"
# Assrt 字幕 API（直连被重置，必须走代理——已实测）
ASSRT_BASE = "https://api.assrt.net/v1/sub"
PROXY = "http://127.0.0.1:7897"
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
                 voice_feedback=None):
        self._config = config or get_config()
        # P1：语音搜电影结果 → UI 反馈回调（MoviePanel 注入；None 时只 print 兜底）
        self._voice_feedback = voice_feedback
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
        self._sub_searched = set()
        # 崩溃自愈状态（对齐音乐链 _try_restart_mpv：防重入 + 3 次/5 分钟上限）
        self._recover_lock = threading.Lock()
        self._recover_pending = False   # 一次恢复进行中（防重入）
        self._recover_count = 0         # 当前窗口内已恢复次数
        self._recover_window_ts = 0.0   # 窗口起点
        self._recover_gave_up = False   # 超限放弃（停止自愈）
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
        except Exception as e:
            logger.error(f"[Movie] 注册语音命令失败: {e}")

    def _cmd_search_play(self, command):
        """语音"播放电影XX"：搜索 → 播 Top1（语音直达，不弹 UI 选集对话框）。
        包2c：params["episode"] 直跳对应集（"播放电影甄嬛传第5集"→第5集）；
        无集数 → 播第一集（现状不变）；集数越界 → 提示 + False（不崩）。"""
        query = ((command.params or {}).get("query") or "").strip()
        if not query:
            return False
        episode = (command.params or {}).get("episode")
        try:
            items = self.search(query)
        except Exception as e:
            self._notify_voice(f"语音搜索失败：{e}")
            return False
        if not items:
            # P1：UI 反馈（面板状态栏显示"未找到：XX"）
            self._notify_voice(f"未找到：{query}")
            return False
        item = items[0]
        if episode is not None:
            ep = int(episode)
            eps = self.episodes(item)
            if len(eps) <= 1:
                if ep == 1:
                    self._notify_voice(f"开始播放：{item.get('vod_name') or ''}")
                    return self.play(item)
                self._notify_voice(f"该片只有 1 集，没有第 {ep} 集")
                return False
            if ep < 1 or ep > len(eps):
                self._notify_voice(f"该剧只有 {len(eps)} 集，没有第 {ep} 集")
                return False
            self._notify_voice(f"开始播放：{item.get('vod_name') or ''} 第{ep}集")
            return self.play(item, index=ep - 1)
        self._notify_voice(f"开始播放：{item.get('vod_name') or ''}")
        return self.play(item)

    def _notify_voice(self, msg: str):
        """语音命令结果 → UI 反馈（回调优先；无回调只 print 兜底）。"""
        cb = getattr(self, "_voice_feedback", None)
        if callable(cb):
            try:
                cb(msg)
                return
            except Exception as e:
                logger.error(f"[Movie] 语音反馈回调异常: {e}")
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
        self.mpv.loadfile(url)
        self._current = item
        self._current_index = idx
        self._last_idle = False  # 新播放开始，等播完再触发连播
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
        if self._current is None or not self._auto_next:
            self._last_idle = self.mpv.get_idle_active()
            return
        if not self.mpv.is_running():
            return
        now_idle = self.mpv.get_idle_active()
        if now_idle and not self._last_idle:
            # 播完一集（idle 上升沿）→ 连播下一集
            self._advance_episode()
        self._last_idle = now_idle

    def _advance_episode(self):
        """切到下一集；无下一集 → 末集停（不循环）。返回 True=已切，False=已停。"""
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
        self.mpv.loadfile(url)
        self._current_index = nxt
        return True

    def stop(self):
        """停电影（幂等；_stopping 守卫防互斥循环）。"""
        if self._stopping:
            return
        self._stopping = True
        try:
            if self.mpv.is_running():
                self.mpv.stop()
        finally:
            self._stopping = False
        self._current = None
        self._current_index = None
        self._last_idle = True
        self._write_state(playing=False, paused=False)

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
            self.mpv.loadfile(url)
            if pos and pos > 0:
                self.mpv.seek(pos, "absolute")
        except Exception as e:
            logger.error(f"[Movie] 崩溃恢复 loadfile 异常: {e!r}")
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
                # 仍占播放权（playing=True），标记暂停态（语音"继续"可恢复）
                self._write_state(paused=True)
        finally:
            self._pausing = False

    def toggle_pause(self):
        self.mpv.toggle_pause()

    def resume(self):
        self.mpv.play()
        if self._current is not None:
            self._write_state(playing=True, paused=False)

    def seek(self, seconds, mode="relative"):
        self.mpv.seek(seconds, mode)

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
                             proxies={"http": PROXY, "https": PROXY})
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
            proxies = {"http": PROXY, "https": PROXY}
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
            self.mpv.sub_add(out_path)
            self._force_select_sub()
            return True, out_path
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
        if self.mpv.is_running():
            self.mpv.set_property("volume", max(0, min(100, int(vol))))

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
