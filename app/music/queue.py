"""
播放队列 - 管理播放列表和播放模式

支持：
- 顺序播放
- 随机播放
- 单曲循环
- 列表循环
"""
import random
import threading
from typing import Any, Callable, Dict, List, Optional


class PlayMode:
    """播放模式"""
    SEQUENTIAL = "sequential"  # 顺序播放
    SHUFFLE = "shuffle"        # 随机播放
    REPEAT_ONE = "repeat_one"  # 单曲循环
    REPEAT_ALL = "repeat_all"  # 列表循环


class PlayQueue:
    """播放队列"""

    def __init__(self):
        self._queue: List[Dict[str, Any]] = []
        self._current_index: int = -1
        self._play_mode: str = PlayMode.SEQUENTIAL
        self._history: List[int] = []  # 播放历史（索引）
        self._history_pos: int = -1
        self._lock = threading.Lock()
        self._shuffle_order: List[int] = []
        self._shuffle_pos: int = -1

    def add(self, song: Dict[str, Any], position: Optional[int] = None):
        """添加歌曲到队列"""
        with self._lock:
            if position is not None and 0 <= position <= len(self._queue):
                self._queue.insert(position, song)
            else:
                self._queue.append(song)
            self._rebuild_shuffle()

    def add_all(self, songs: List[Dict[str, Any]]):
        """批量添加歌曲"""
        with self._lock:
            self._queue.extend(songs)
            self._rebuild_shuffle()

    def remove(self, index: int):
        """移除指定位置的歌曲"""
        with self._lock:
            if 0 <= index < len(self._queue):
                self._queue.pop(index)
                if index < self._current_index:
                    self._current_index -= 1
                elif index == self._current_index:
                    self._current_index = -1
                self._rebuild_shuffle()

    def clear(self):
        """清空队列"""
        with self._lock:
            self._queue.clear()
            self._current_index = -1
            self._history.clear()
            self._history_pos = -1
            self._shuffle_order.clear()
            self._shuffle_pos = -1

    def get_current(self) -> Optional[Dict[str, Any]]:
        """获取当前播放的歌曲"""
        with self._lock:
            if 0 <= self._current_index < len(self._queue):
                return self._queue[self._current_index]
            return None

    def get_next(self) -> Optional[Dict[str, Any]]:
        """获取下一首歌曲（不改变当前位置）"""
        with self._lock:
            next_index = self._get_next_index()
            if next_index is not None and 0 <= next_index < len(self._queue):
                return self._queue[next_index]
            return None

    def next(self) -> Optional[Dict[str, Any]]:
        """播放下一首，返回歌曲"""
        with self._lock:
            next_index = self._get_next_index()
            if next_index is not None:
                self._current_index = next_index
                self._add_history(next_index)
                return self._queue[next_index]
            return None

    def prev(self) -> Optional[Dict[str, Any]]:
        """播放上一首，返回歌曲"""
        with self._lock:
            # 优先从历史记录回退
            if self._history_pos > 0:
                self._history_pos -= 1
                prev_index = self._history[self._history_pos]
                if 0 <= prev_index < len(self._queue):
                    self._current_index = prev_index
                    return self._queue[prev_index]

            # 顺序模式的上一首
            if self._play_mode in (PlayMode.SEQUENTIAL, PlayMode.REPEAT_ALL):
                if self._current_index > 0:
                    self._current_index -= 1
                    self._add_history(self._current_index)
                    return self._queue[self._current_index]
                elif self._play_mode == PlayMode.REPEAT_ALL and self._queue:
                    self._current_index = len(self._queue) - 1
                    self._add_history(self._current_index)
                    return self._queue[self._current_index]

            return None

    def play_at(self, index: int) -> Optional[Dict[str, Any]]:
        """播放指定位置的歌曲"""
        with self._lock:
            if 0 <= index < len(self._queue):
                self._current_index = index
                self._add_history(index)
                return self._queue[index]
            return None

    def _get_next_index(self) -> Optional[int]:
        """计算下一首的索引"""
        if not self._queue:
            return None

        if self._play_mode == PlayMode.REPEAT_ONE:
            return self._current_index if self._current_index >= 0 else 0

        if self._play_mode == PlayMode.SHUFFLE:
            return self._get_shuffle_next()

        # SEQUENTIAL / REPEAT_ALL
        if self._current_index < 0:
            return 0

        next_index = self._current_index + 1
        if next_index >= len(self._queue):
            if self._play_mode == PlayMode.REPEAT_ALL:
                return 0
            return None  # 顺序播放到末尾

        return next_index

    def _get_shuffle_next(self) -> Optional[int]:
        """随机播放的下一首"""
        if not self._shuffle_order:
            return None

        self._shuffle_pos += 1
        if self._shuffle_pos >= len(self._shuffle_order):
            # 重新洗牌
            self._rebuild_shuffle()
            self._shuffle_pos = 0

        return self._shuffle_order[self._shuffle_pos]

    def _rebuild_shuffle(self):
        """重建随机播放顺序"""
        self._shuffle_order = list(range(len(self._queue)))
        random.shuffle(self._shuffle_order)
        self._shuffle_pos = -1

    def _add_history(self, index: int):
        """添加到播放历史"""
        # 截断历史位置之后的记录
        if self._history_pos < len(self._history) - 1:
            self._history = self._history[:self._history_pos + 1]
        self._history.append(index)
        self._history_pos = len(self._history) - 1
        # 限制历史长度
        if len(self._history) > 100:
            self._history = self._history[-100:]
            self._history_pos = len(self._history) - 1

    def set_play_mode(self, mode: str):
        """设置播放模式"""
        with self._lock:
            if mode in (PlayMode.SEQUENTIAL, PlayMode.SHUFFLE,
                        PlayMode.REPEAT_ONE, PlayMode.REPEAT_ALL):
                self._play_mode = mode
                if mode == PlayMode.SHUFFLE:
                    self._rebuild_shuffle()

    def get_play_mode(self) -> str:
        """获取播放模式"""
        return self._play_mode

    def get_queue(self) -> List[Dict[str, Any]]:
        """获取整个队列"""
        with self._lock:
            return list(self._queue)

    def get_current_index(self) -> int:
        """获取当前播放索引"""
        return self._current_index

    def size(self) -> int:
        """队列长度"""
        return len(self._queue)

    def is_empty(self) -> bool:
        """队列是否为空"""
        return len(self._queue) == 0
