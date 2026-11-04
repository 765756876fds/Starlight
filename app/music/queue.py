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
from typing import Any


class PlayMode:
    """播放模式"""
    SEQUENTIAL = "sequential"  # 顺序播放
    SHUFFLE = "shuffle"        # 随机播放
    REPEAT_ONE = "repeat_one"  # 单曲循环
    REPEAT_ALL = "repeat_all"  # 列表循环


class PlayQueue:
    """播放队列"""

    def __init__(self):
        self._queue: list[dict[str, Any]] = []
        self._current_index: int = -1
        self._play_mode: str = PlayMode.SEQUENTIAL
        self._history: list[int] = []  # 播放历史（索引）
        self._history_pos: int = -1
        self._lock = threading.Lock()
        self._shuffle_order: list[int] = []
        self._shuffle_pos: int = -1
        self._next_up: list[int] = []  # 明确的下一首请求，后选先播；仍引用同一队列。

    def _insert(self, song: dict[str, Any], position: int):
        """持锁插入并移动索引；保持当前曲目、历史及随机遍历对应同一歌曲。"""
        self._queue.insert(position, song)
        if self._current_index >= position:
            self._current_index += 1
        self._history = [i + (i >= position) for i in self._history]
        self._next_up = [i + (i >= position) for i in self._next_up]
        self._shuffle_order = [i + (i >= position) for i in self._shuffle_order]

    def add(self, song: dict[str, Any], position: int | None = None):
        """添加歌曲到队列"""
        with self._lock:
            if position is not None and 0 <= position <= len(self._queue):
                self._insert(song, position)
            else:
                self._queue.append(song)
            self._rebuild_shuffle()

    def add_next(self, song: dict[str, Any]):
        """原子插到当前曲目后；跨模式优先消费一次，连续请求后选先播。"""
        with self._lock:
            position = self._current_index + 1
            self._insert(song, position)
            self._next_up.insert(0, position)
            # 不重洗现有随机顺序；插队项不重复进入本轮随机遍历。

    def add_all(self, songs: list[dict[str, Any]]):
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
                self._history_pos = sum(i != index for i in self._history[:self._history_pos + 1]) - 1
                self._history = [i - (i > index) for i in self._history if i != index]
                self._next_up = [i - (i > index) for i in self._next_up if i != index]
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
            self._next_up.clear()

    def get_current(self) -> dict[str, Any] | None:
        """获取当前播放的歌曲"""
        with self._lock:
            if 0 <= self._current_index < len(self._queue):
                return self._queue[self._current_index]
            return None

    def get_next(self) -> dict[str, Any] | None:
        """获取下一首歌曲（不改变当前位置）"""
        with self._lock:
            next_index = self._get_next_index()
            if next_index is not None and 0 <= next_index < len(self._queue):
                return self._queue[next_index]
            return None

    def next(self, auto: bool = False) -> dict[str, Any] | None:
        """播放下一首，返回歌曲。auto=True=自然结束（单曲重播），False=用户手动。"""
        with self._lock:
            next_index = self._get_next_index(auto=auto)
            if next_index is not None:
                if next_index in self._next_up:
                    self._next_up.remove(next_index)
                self._current_index = next_index
                self._add_history(next_index)
                return self._queue[next_index]
            return None

    def skip_current(self) -> dict[str, Any] | None:
        """跳过当前歌曲：顺序取下一首（REPEAT_ONE 下也不循环回当前）。

        仅用于"单曲循环中当前文件不可用"的逃生路径；返回 None 表示队列到尾。
        """
        with self._lock:
            if not self._queue:
                return None
            nxt = self._next_up[0] if self._next_up else self._current_index + 1
            if nxt >= len(self._queue):
                if self._play_mode == PlayMode.REPEAT_ALL:
                    nxt = 0
                else:
                    return None
            if nxt in self._next_up:
                self._next_up.remove(nxt)
            self._current_index = nxt
            self._add_history(nxt)
            return self._queue[nxt]

    def prev(self) -> dict[str, Any] | None:
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

    def play_at(self, index: int) -> dict[str, Any] | None:
        """播放指定位置的歌曲"""
        with self._lock:
            if 0 <= index < len(self._queue):
                if index in self._next_up:
                    self._next_up.remove(index)
                self._current_index = index
                self._add_history(index)
                return self._queue[index]
            return None

    def _get_next_index(self, auto: bool = False) -> int | None:
        """计算下一首的索引。auto=True=自然结束（单曲重播当前），False=用户手动。"""
        if not self._queue:
            return None

        if self._next_up:
            return self._next_up[0]

        if self._play_mode == PlayMode.REPEAT_ONE and auto:
            return max(self._current_index, 0)

        if self._play_mode == PlayMode.REPEAT_ONE and not auto:
            # 手动切歌：单曲模式下也跳到下一首
            next_index = self._current_index + 1
            if next_index >= len(self._queue):
                next_index = 0
            return next_index

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

    def _get_shuffle_next(self) -> int | None:
        """随机播放的下一首"""
        if not self._shuffle_order:
            self._rebuild_shuffle()
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
        pending = set(self._next_up)
        self._shuffle_order = [i for i in range(len(self._queue)) if i not in pending]
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

    def get_queue(self) -> list[dict[str, Any]]:
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
