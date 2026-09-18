"""Auto Accept：严格按 Syronss bot.py 的四步流程。"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional


READY_CHECK_URL = "/lol-matchmaking/v1/ready-check"
ACCEPT_URL = "/lol-matchmaking/v1/ready-check/accept"


class ReadyCheckController:
    def __init__(
        self,
        request_with_status: Callable[[str, str, Optional[dict]], tuple],
        stop_event: threading.Event,
        on_event: Callable[[str, dict], None],
    ):
        self._request = request_with_status
        self._stop = stop_event
        self._on_event = on_event
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def check(self, accept_delay: float) -> None:
        """一个 polling tick。完整实现 Syronss 流程。"""
        status, payload = self._request("GET", READY_CHECK_URL, None)
        if status != 200 or not isinstance(payload, dict):
            self._active = False
            return

        in_progress = payload.get("state") == "InProgress"
        unanswered = payload.get("playerResponse") == "None"
        if not in_progress or not unanswered:
            self._active = False
            return

        # 同一 ready-check 不重复触发
        if self._active:
            return
        self._active = True
        ready_check_id = payload.get("readyCheckId")
        self._on_event("automation.ready_check_detected", {"ready_check_id": ready_check_id})

        # 步骤 2：可中断等待
        if accept_delay and self._stop.wait(accept_delay):
            return
        if self._stop.is_set():
            return

        # 步骤 3：重新校验
        status2, current = self._request("GET", READY_CHECK_URL, None)
        if status2 != 200 or not isinstance(current, dict):
            self._active = False
            self._on_event("automation.ready_check_expired", {"reason": "revalidate_failed"})
            return

        same_id = (
            ready_check_id is None
            or current.get("readyCheckId") is None
            or current.get("readyCheckId") == ready_check_id
        )
        if (
            current.get("state") != "InProgress"
            or current.get("playerResponse") != "None"
            or not same_id
        ):
            self._active = False
            self._on_event("automation.ready_check_expired", {"reason": "state_changed"})
            return

        # 步骤 4：accept
        accept_status, _ = self._request("POST", ACCEPT_URL, None)
        if accept_status in (200, 204):
            self._on_event("automation.accept_sent", {"ready_check_id": ready_check_id})
        else:
            self._on_event(
                "automation.error",
                {"what": "accept_failed", "status": accept_status},
            )
        self._active = False

    def reset(self) -> None:
        self._active = False
