"""播放历史记录器：订阅 MUSIC_TRACK_CHANGED，把换歌写进 play_history。

只存元数据（title/artist/source），不缓存歌曲文件。
去重：与最近一条比对，5 分钟内同一首歌（title+artist+source_type）不重复记，
防单曲循环刷屏。
"""
import time
from typing import Any

from ..core.event_bus import EventType


class HistoryRecorder:
    """独立模块：构造持 db/event_bus，start() 订阅换歌事件（幂等）。

    - 装配点：由 MusicController.start() 调用 start()（与 _subscribe_speech_events
      同一自订阅模式），不依赖 main.py；
    - 本地歌：music.id > 0 → source_type=local, music_id=id；
    - 在线歌：song.source == "online" 或 id < 0 → source_type=online，
      source_name/track_id 取自事件 payload（controller 在线路径已补发）。
    """

    DEDUP_WINDOW_S = 300  # 5 分钟

    def __init__(self, db, event_bus):
        self._db = db
        self._event_bus = event_bus
        self._started = False

    def start(self) -> None:
        """订阅换歌事件（幂等：重复 start 只订一次）。"""
        if self._started:
            return
        self._started = True
        try:
            self._event_bus.subscribe(EventType.MUSIC_TRACK_CHANGED,
                                      self._on_track_changed)
        except Exception as e:
            print(f"[History] 订阅换歌事件失败: {e}")

    def stop(self) -> None:
        """取消订阅（幂等）。"""
        if not self._started:
            return
        self._started = False
        try:
            self._event_bus.unsubscribe(EventType.MUSIC_TRACK_CHANGED,
                                        self._on_track_changed)
        except Exception:
            pass

    def _on_track_changed(self, event) -> None:
        try:
            song = (event.data or {}).get("song")
            if not song or not isinstance(song, dict):
                return
            self.record_song(song)
        except Exception as e:
            print(f"[History] 记录播放历史失败: {e}")

    def record_song(self, song: dict[str, Any]) -> None:
        """公开记录入口：事件订阅与 play_online 直接调用共用。

        去重键：5 分钟内同 title+artist+source_type 视为同一首歌，不重复记
        （track_id/source_name 不参与——同一首曲子无论换源还是重播都不刷屏）。
        """
        title = str(song.get("title") or "").strip()
        if not title:
            return
        artist = str(song.get("artist") or "").strip()
        sid = int(song.get("id") or 0)
        is_online = bool(song.get("source") == "online") or sid < 0
        if is_online:
            source_type = "online"
            source_name = str(song.get("source_name") or "")
            track_id = str(song.get("track_id") or "")
            music_id = 0
        else:
            if sid <= 0:
                return  # 本地 id 缺失/无效，无法重播，不记
            source_type = "local"
            source_name = ""
            track_id = ""
            music_id = sid

        # 去重：5 分钟内同一首歌不重复记（防单曲循环刷屏）
        try:
            recent = self._db.get_history(limit=1)
            now = time.time()
            if recent:
                r = recent[0]
                if (now - float(r.get("played_at") or 0)) < self.DEDUP_WINDOW_S \
                        and r.get("title") == title \
                        and r.get("artist") == artist \
                        and r.get("source_type") == source_type:
                    return
        except Exception:
            pass

        self._db.add_history(
            title=title,
            artist=artist,
            source_type=source_type,
            source_name=source_name,
            track_id=track_id,
            music_id=music_id,
        )
