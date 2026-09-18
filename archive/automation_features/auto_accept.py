"""auto_accept：自动接受匹配。参考 Syronss bot.py + LeagueAkari action-controller.ts。"""
from __future__ import annotations

from .base import Feature, FeatureResult
from ..ready_check_controller import ReadyCheckController


class AutoAcceptFeature(Feature):
    name = "auto_accept"

    def __init__(self, request_with_status, stop_event, on_event,
                 accept_delay: float = 0.0, enabled: bool = False):
        super().__init__(enabled=enabled)
        self._ctl = ReadyCheckController(request_with_status, stop_event, on_event)
        self.accept_delay = accept_delay

    def tick(self, session, config) -> FeatureResult:
        if not self.enabled:
            return FeatureResult()
        phase = getattr(session, "phase", None) if session is not None else None
        if phase != "ReadyCheck":
            return FeatureResult()
        self._ctl.check(self.accept_delay)
        # ReadyCheckController 内部发 accept_sent 事件；这里只记录 tick
        return FeatureResult(detail={"phase": phase})
