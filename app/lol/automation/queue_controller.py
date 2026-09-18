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
        self._prompted_user = False

    def is_currently_in_queue(self) -> bool:
        status, payload = self._request("GET", SEARCH_STATE_URL, None)
        if status == 200 and isinstance(payload, dict):
            return bool(payload.get("isCurrentlyInQueue"))
        return False

    def try_start(self, gameflow_phase: str, config=None) -> bool:
        """True=刚 POST search；False=无需操作或等待。"""
        if self.is_currently_in_queue():
            self._search_posted = True
            return False

        if gameflow_phase == "Matchmaking":
            self._search_posted = True
            return False

        if gameflow_phase == "Lobby":
            self._prompted_user = False
            if self._search_posted:
                return False
            status, _ = self._request("POST", SEARCH_URL, None)
            if status in (200, 204):
                self._search_posted = True
                self._on_event("automation.queue_search_started", {})
                return True
            self._on_event("automation.error", {"what": "search_failed", "status": status})
            return False

        if gameflow_phase in ("None", "LobbyClient", ""):
            if not self._prompted_user:
                self._prompted_user = True
                self._on_event("automation.needs_lobby", {
                    "hint": "请点击 Play → 选择模式 → 创建房间，程序会自动排队"
                })
                print("[Queue] 空闲：请手动创建房间，程序将自动排队")
            return False

        self._search_posted = False
        self._prompted_user = False
        return False
