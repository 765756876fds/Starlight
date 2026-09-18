"""safe_stop：安全停止。参考 Syronss bot.py stop + LeagueAkari cancelAutoMatchmaking。"""
from __future__ import annotations

import threading
from typing import Callable, List

from .base import Feature, FeatureResult


class SafeStopFeature(Feature):
    name = "safe_stop"

    def __init__(self, stop_event: threading.Event,
                 cleaners: List[Callable[[], None]] | None = None,
                 enabled: bool = True):
        super().__init__(enabled=enabled)
        self._stop = stop_event
        self._cleaners: List[Callable[[], None]] = list(cleaners or [])

    def register_cleaner(self, fn: Callable[[], None]) -> None:
        self._cleaners.append(fn)

    def emergency_stop(self) -> None:
        self._stop.set()
        for fn in self._cleaners:
            try:
                fn()
            except Exception:
                pass

    def stop(self) -> None:
        self.emergency_stop()

    def tick(self, session, config) -> FeatureResult:
        if self._stop.is_set():
            return FeatureResult(detail={"stopped": True})
        return FeatureResult()
