"""
音乐控制器 - 高层封装，整合 mpv + 队列 + 数据库 + 搜索

对外提供统一的音乐操作接口。
通过 EventBus 发布音乐事件。
通过 CommandBus 接收音乐命令。
"""
import os
import threading
import time
from typing import Any, Dict, List, Optional

from ..core.event_bus import EventBus, Event, EventType, get_event_bus
from ..core.command_bus import CommandBus, Command, CommandType, get_command_bus
from ..core.state_store import StateStore, StateNamespace, get_state_store

from .database import MusicDatabase
from .scanner import MusicScanner
from .mpv import MpvController
from .queue import PlayQueue, PlayMode
from .search import MusicSearch


class MusicController:
    """
    音乐控制器

    整合所有音乐子模块，提供统一接口。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.db_path = config.get("database", "./data/music.db")
        self.scan_dirs = config.get("scan_dirs", [])
        self.extensions = set(config.get("extensions", [".mp3", ".flac", ".wav", ".m4a", ".ogg"]))
        self.mpv_path = config.get("mpv_path", "mpv")
        self.mpv_ipc_pipe = config.get("mpv_ipc_pipe", "\\\\.\\pipe\\lol_music_mpv")
        self.default_volume = config.get("default_volume", 70)

        # 初始化子模块
        self.db = MusicDatabase(self.db_path)
        self.scanner = MusicScanner(self.db, self.scan_dirs, self.extensions)
        self.search = MusicSearch(self.db)
        self.queue = PlayQueue()
        self.mpv = MpvController(self.mpv_path, self.mpv_ipc_pipe)

        # 核心服务
        self.event_bus = get_event_bus()
        self.command_bus = get_command_bus()
        self.state_store = get_state_store()

        # 状态
        self._running = False
        self._lock = threading.Lock()
        self._current_song: Optional[Dict[str, Any]] = None

        # 注册命令处理器
        self._register_commands()

        # 注册 mpv 事件监听
        self.mpv.on_property_change("idle-active", self._on_idle_change)
        self.mpv.on_property_change("eof-reached", self._on_eof)

    def start(self):
        """启动音乐模块"""
        if self._running:
            return

        # 启动 mpv
        self.mpv.start()
        self.mpv.set_volume(self.default_volume)

        # 注册事件
        self._running = True

        # 更新状态
        self.state_store.set(StateNamespace.MUSIC, "volume", self.default_volume)
        self.state_store.set(StateNamespace.MUSIC, "playing", False)
        self.state_store.set(StateNamespace.MUSIC, "current_song", None)

        print("[Music] 模块已启动")

    def stop(self):
        """停止音乐模块"""
        self._running = False
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
        self.command_bus.register(CommandType.MUSIC_RESCAN, lambda c: self.rescan())

    # ===== 播放控制 =====

    def play(self):
        """播放"""
        if self.queue.is_empty():
            # 队列空，随机播放
            songs = self.db.get_random_songs(20)
            if songs:
                self.queue.add_all(songs)
                self._play_song(self.queue.next())
        else:
            self.mpv.play()
            self._update_state(playing=True)

    def pause(self):
        """暂停"""
        self.mpv.pause()
        self._update_state(playing=False)

    def resume(self):
        """继续播放"""
        self.mpv.play()
        self._update_state(playing=True)

    def toggle(self):
        """切换播放/暂停"""
        if self.mpv.is_paused():
            self.resume()
        else:
            self.pause()

    def next(self):
        """下一首"""
        with self._lock:
            song = self.queue.next()
            if song:
                self._play_song(song)
            else:
                # 队列结束，随机补充
                songs = self.db.get_random_songs(10)
                if songs:
                    self.queue.add_all(songs)
                    song = self.queue.next()
                    if song:
                        self._play_song(song)

    def prev(self):
        """上一首"""
        with self._lock:
            song = self.queue.prev()
            if song:
                self._play_song(song)

    def play_song(self, song: Dict[str, Any]):
        """播放指定歌曲"""
        with self._lock:
            # 添加到队列并播放
            self.queue.add(song)
            self.queue.play_at(self.queue.size() - 1)
            self._play_song(song)

    def play_search_result(self, query: str, index: int = 0):
        """搜索并播放第 N 个结果"""
        results = self.search.search(query, limit=10)
        if results and 0 <= index < len(results):
            self.play_song(results[index])
            return results[index]
        return None

    def _play_song(self, song: Dict[str, Any]):
        """实际播放歌曲"""
        if not song:
            return

        filepath = song.get("path")
        if not filepath or not os.path.exists(filepath):
            print(f"[Music] 文件不存在: {filepath}")
            return

        self.mpv.loadfile(filepath)
        self._current_song = song

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

    # ===== 音量控制 =====

    def set_volume(self, volume: int):
        """设置音量"""
        volume = max(0, min(100, volume))
        self.mpv.set_volume(volume)
        self.state_store.set(StateNamespace.MUSIC, "volume", volume)

    def volume_up(self, step: int = 5):
        """音量增加"""
        current = self.mpv.get_volume()
        self.set_volume(current + step)

    def volume_down(self, step: int = 5):
        """音量减少"""
        current = self.mpv.get_volume()
        self.set_volume(current - step)

    def get_volume(self) -> int:
        """获取当前音量"""
        return self.mpv.get_volume()

    # ===== 跳转 =====

    def seek(self, seconds: float, mode: str = "relative"):
        """跳转"""
        self.mpv.seek(seconds, mode)

    def jump_to_highlight(self):
        """跳到高潮部分"""
        if not self._current_song:
            return

        song_id = self._current_song.get("id")
        highlight = self.db.get_highlight(song_id)
        if highlight:
            self.mpv.seek(highlight["start_time"], "absolute")
            self.event_bus.publish(Event(
                event_type=EventType.MUSIC_HIGHLIGHT,
                data={"song_id": song_id, "position": highlight["start_time"]},
                source="music"
            ))
        else:
            # 没有高潮数据，跳到 60% 位置作为兜底
            duration = self.mpv.get_duration()
            if duration > 0:
                self.mpv.seek(duration * 0.6, "absolute")

    # ===== 扫描 =====

    def rescan(self, full: bool = False) -> Dict[str, int]:
        """重新扫描音乐库"""
        print(f"[Music] 开始扫描音乐库 (full={full})...")
        result = self.scanner.scan(full_rescan=full)
        print(f"[Music] 扫描完成: {result}")
        self.state_store.set(StateNamespace.MUSIC, "song_count", self.db.get_song_count())
        return result

    def get_song_count(self) -> int:
        """获取歌曲总数"""
        return self.db.get_song_count()

    # ===== 队列管理 =====

    def get_queue(self) -> List[Dict[str, Any]]:
        """获取播放队列"""
        return self.queue.get_queue()

    def clear_queue(self):
        """清空队列"""
        self.queue.clear()

    def set_play_mode(self, mode: str):
        """设置播放模式"""
        self.queue.set_play_mode(mode)

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
        if self._running and self.mpv.is_idle():
            self.next()

    def _update_state(self, **kwargs):
        """更新状态存储"""
        for key, value in kwargs.items():
            self.state_store.set(StateNamespace.MUSIC, key, value)

    def is_playing(self) -> bool:
        """是否正在播放"""
        return not self.mpv.is_paused() and not self.mpv.is_idle()
