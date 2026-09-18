"""auto_queue：自动开始匹配。参考 LeagueAkari matchmaking-controller.ts。"""
from __future__ import annotations

from typing import Any, Optional

from .base import Feature, FeatureResult
from ..queue_controller import QueueController


class AutoQueueFeature(Feature):
    name = "auto_queue"

    def __init__(self, request_with_status, on_event, enabled: bool = False):
        super().__init__(enabled=enabled)
        self._ctl = QueueController(request_with_status, on_event)

    def tick(self, session, config) -> FeatureResult:
        if not self.enabled:
            return FeatureResult()
        phase = getattr(session, "phase", None) if session is not None else None
        if phase != "Lobby":
            return FeatureResult()
        if self._ctl.is_currently_in_queue():
            return FeatureResult(detail={"reason": "already_in_queue"})
        ok = self._ctl.try_start(phase)
        if ok:
            return self._record_action(FeatureResult(
                action="post",
                endpoint="/lol-lobby/v2/lobby/matchmaking/search",
                status_code=204,
                detail={"phase": phase},
            ))
        return FeatureResult(detail={"phase": phase})
