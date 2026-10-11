"""PUUID → PlayerSnapshot 内存缓存（TTL）。"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from .models import PlayerSnapshot


class SnapshotCache:
    # TTL 覆盖整局：LOL 对局最长约 1 小时，默认 5 分钟会导致对局中段快照全部过期，
    # UI 直接读 loader._cache → 段位/绝活全部消失（"暂无数据"）。
    # 跨局污染由新局加载 put 覆盖（同 puuid 新快照直接替换）。
    def __init__(self, ttl_seconds: float = 7200.0):
        self._ttl = ttl_seconds
        self._data: Dict[str, PlayerSnapshot] = {}
        self._name_index: Dict[str, str] = {}  # display_name → puuid 反查索引
        self._lock = threading.Lock()

    def get(self, puuid: str) -> Optional[PlayerSnapshot]:
        with self._lock:
            s = self._data.get(puuid)
            if s is None:
                return None
            if time.monotonic() - s.loaded_at > self._ttl:
                return None
            return s

    def get_by_display_name(self, display_name: str) -> Optional[PlayerSnapshot]:
        """按 display_name 反查 PlayerSnapshot（本局匹配用）。"""
        if not display_name:
            return None
        # 最小规范化：去首尾空白
        key = display_name.strip()
        with self._lock:
            puuid = self._name_index.get(key)
            if not puuid:
                return None
            s = self._data.get(puuid)
            if s is None:
                return None
            if time.monotonic() - s.loaded_at > self._ttl:
                return None
            return s

    def put(self, snap: PlayerSnapshot) -> None:
        with self._lock:
            self._data[snap.puuid] = snap
            # 同步维护 display_name → puuid 索引
            if snap.display_name:
                full = snap.display_name.strip()
                self._name_index[full] = snap.puuid
                # 同时索引拆掉 #tag 的纯名字（liveclient riotId 形如"名字#tag"，
                # 而部分 loader 快照 display_name 无 tag；双向兼容保证 UI 能匹配）
                base = full.split("#")[0].strip()
                if base and base != full:
                    self._name_index.setdefault(base, snap.puuid)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._name_index.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._data)

    def all(self):
        """返回所有未过期快照（用于检测缺失历史战绩）。"""
        now = time.monotonic()
        with self._lock:
            return [s for s in self._data.values()
                    if s is not None and now - s.loaded_at <= self._ttl]
