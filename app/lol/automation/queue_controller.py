"""Auto Queue：启动匹配前做硬性前置校验。"""
from __future__ import annotations

from typing import Any, Callable, Optional


SEARCH_URL = "/lol-lobby/v2/lobby/matchmaking/search"
SEARCH_STATE_URL = "/lol-lobby/v2/lobby/matchmaking/search-state"


class QueueController:
    def __init__(
        self,
        request_with_status: Callable[[str, str, Optional[dict]], tuple],
        on_event: Callable[[str, dict], None],
    ):
        self._request = request_with_status
        self._on_event = on_event
        self._search_posted = False

    def is_currently_in_queue(self) -> bool:
        status, payload = self._request("GET", SEARCH_STATE_URL, None)
        if status == 200 and isinstance(payload, dict):
            return bool(payload.get("isCurrentlyInQueue"))
        return False

    def try_start(self, gameflow_phase: str) -> bool:
        # 离开 Lobby 清标志，下次再进 Lobby 时可以重新 POST
        if gameflow_phase != "Lobby":
            self._search_posted = False
            return False

        # 本次 Lobby 内已经 POST 过 search，不重复
        if self._search_posted:
            return False

        # 兼容：LCU 已经反映为排队中
        if self.is_currently_in_queue():
            self._search_posted = True
            return False

        status, _ = self._request("POST", SEARCH_URL, None)
        if status in (200, 204):
            self._search_posted = True
            self._on_event("automation.queue_search_started", {})
            return True
        self._on_event(
            "automation.error",
            {"what": "search_failed", "status": status},
        )
        return False
