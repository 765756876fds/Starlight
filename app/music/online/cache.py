"""内存缓存（TTL 5 分钟）。"""
from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple

from .models import OnlineTrack


class OnlineCache:
    """内存缓存（TTL 5 分钟，容量上限 MAX_ENTRIES，过期/超量自动清理）。"""

    MAX_ENTRIES = 200

    def __init__(self, ttl_seconds: float = 300.0):
        self._ttl = ttl_seconds
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, source: str, keyword: str, limit: int) -> Optional[List[OnlineTrack]]:
        key = (source, keyword.lower(), limit)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, tracks = entry
            if time.monotonic() - ts > self._ttl:
                return None
            return list(tracks)

    def set(self, source: str, keyword: str, limit: int, tracks: List[OnlineTrack]) -> None:
        key = (source, keyword.lower(), limit)
        with self._lock:
            now = time.monotonic()
            # 先清理过期条目（防 TTL 失效条目长期堆积 → 内存膨胀）
            stale = [k for k, (ts, _) in self._data.items()
                     if now - ts > self._ttl]
            for k in stale:
                del self._data[k]
            self._data[key] = (now, list(tracks))
            # 容量上限：超出时移除最早插入的条目（dict 保持插入序）
            while len(self._data) > self.MAX_ENTRIES:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def delete(self, source: str) -> None:
        """删除指定音源的全部缓存条目（音源注销/替换时调用）。"""
        with self._lock:
            stale = [k for k in self._data if k[0] == source]
            for k in stale:
                del self._data[k]
