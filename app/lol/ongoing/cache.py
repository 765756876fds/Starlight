"""PUUID → PlayerSnapshot 内存缓存（TTL）。"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from .models import PlayerSnapshot


class SnapshotCache:
    def __init__(self, ttl_seconds: float = 300.0):
        self._ttl = ttl_seconds
        self._data: Dict[str, PlayerSnapshot] = {}
        self._lock = threading.Lock()

    def get(self, puuid: str) -> Optional[PlayerSnapshot]:
        with self._lock:
            s = self._data.get(puuid)
            if s is None:
                return None
            if time.monotonic() - s.loaded_at > self._ttl:
                return None
            return s

    def put(self, snap: PlayerSnapshot) -> None:
        with self._lock:
            self._data[snap.puuid] = snap

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._data)
