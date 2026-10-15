"""
电影链控制器 - 苹果 CMS 搜索 + mpv 独立视频实例播放。

- 与音乐链互斥：播电影前停音乐（直接调 music.stop()）；音乐开始播新歌时
  电影订阅 MUSIC_TRACK_CHANGED 事件自行停止（事件解耦，音乐链零改动）。
- mpv 独立实例（video=True 出视频窗口，taskkill=False 不杀音乐实例）。
"""
import json
import os
import urllib.parse

import requests

from ..core.config import get_config
from ..core.event_bus import EventType, get_event_bus
from ..music.mpv import MpvController

# 默认源：360资源（苹果 CMS 标准接口）
DEFAULT_SOURCE_URL = "https://360zyzz.com/api.php/provide/vod/from/360m3u8/at/json"
DEFAULT_IPC_PIPE = r"\\.\pipe\lol_movie_mpv"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


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


class MovieController:
    """搜索 + 播放控制 + 与音乐互斥（事件解耦）。"""

    def __init__(self, source_url=None, mpv_path=None, ipc_pipe=None,
                 music=None, config=None):
        self._config = config or get_config()
        self.source_url = source_url or self._config.get("movie.source_url") or DEFAULT_SOURCE_URL
        mpv_bin = mpv_path or self._config.get("music.mpv_path") or "mpv"
        pipe = ipc_pipe or self._config.get("movie.mpv_ipc_pipe") or DEFAULT_IPC_PIPE
        # 断点续播目录（仅电影实例；音乐链不加——用户明确不要）
        watch_dir = (self._config.get("movie.watch_later_dir")
                     or os.path.join(self._config.get("app.data_dir", "./data"),
                                     "movie", "watch_later"))
        try:
            os.makedirs(watch_dir, exist_ok=True)
        except Exception:
            pass
        self.mpv = MpvController(mpv_bin, pipe, video=True, watch_later_dir=watch_dir)
        self._music = music
        self._current = None
        self._stopping = False
        self._pausing = False
        self._subscribed = False
        self._subscribe()

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
            print(f"[Movie] 订阅音乐事件失败: {e}")

    def _on_music_playing(self, event):
        """音乐开始播放（换歌或继续）→ 电影暂停（保留进度）。幂等 + 守卫防循环。"""
        self.pause()

    def _on_shutdown(self, event):
        """软件退出 → 杀电影 mpv（与音乐链 stop() 行为一致）。"""
        self.close()

    # ---- 搜索 ----

    def search(self, keyword, timeout=10.0):
        """苹果 CMS 搜索。返回 [{vod_id, vod_name, vod_remarks, play_url,
        first_name, first_url}]；无结果返回 []；网络/解析失败抛异常（UI 层降级提示）。"""
        if not keyword or not keyword.strip():
            return []
        params = {"ac": "videolist", "wd": keyword.strip()}
        r = requests.get(self.source_url, params=params, timeout=timeout, headers=UA)
        data = parse_cms_response(r.text)
        items = []
        for v in data.get("list", []) or []:
            pu = v.get("vod_play_url") or ""
            first = first_play_url(pu)
            items.append({
                "vod_id": v.get("vod_id"),
                "vod_name": v.get("vod_name", "") or "",
                "vod_remarks": v.get("vod_remarks", "") or "",
                "play_url": pu,
                "first_name": first[0] if first else "",
                "first_url": first[1] if first else "",
            })
        return items

    # ---- 播放 ----

    def play(self, item):
        """播电影：先停音乐（互斥），再起视频 mpv 播第一条 URL。失败返回 False。"""
        if not isinstance(item, dict):
            return False
        url = item.get("first_url") or ""
        if not url:
            print(f"[Movie] 无可用播放地址: {item.get('vod_name')}")
            return False
        # 互斥 1：电影 → 音乐（暂停，保留队列与进度；music.pause() 不发事件，无循环）
        music = self._music
        if music is not None:
            try:
                music.pause()
            except Exception as e:
                print(f"[Movie] 暂停音乐失败: {e}")
        # 独立视频实例：taskkill=False 不杀音乐实例；video=True 出视频窗口
        if not self.mpv.is_running():
            if not self.mpv.start(taskkill=False):
                print("[Movie] mpv 启动失败")
                return False
        self.mpv.loadfile(url)
        self._current = item
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

    # ---- 播放控制（复用 mpv JSON IPC） ----

    def pause(self):
        """暂停电影（保留进度；_pausing 守卫防互斥循环）。"""
        if self._pausing:
            return
        self._pausing = True
        try:
            if self.mpv.is_running():
                self.mpv.pause()
        finally:
            self._pausing = False

    def toggle_pause(self):
        self.mpv.toggle_pause()

    def resume(self):
        self.mpv.play()

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
