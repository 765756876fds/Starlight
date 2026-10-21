"""Auto Accept：严格的 ready-check 四步流程 + accept 后复核。

流程：预检 InProgress → 可中断等待 configured delay → 等待后复核状态未变
→ POST accept → accept 后复核（应离开 InProgress）。
"""
from __future__ import annotations

import threading

READY_CHECK_URL = "/lol-matchmaking/v1/ready-check"
ACCEPT_URL = "/lol-matchmaking/v1/ready-check/accept"


class ReadyCheckController:
    def __init__(
        self,
        request_with_status,
        stop_event: threading.Event,
        on_event,
    ):
        self._request = request_with_status
        self._stop = stop_event
        self._on_event = on_event
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def check(self, accept_delay: float) -> None:
        # 1) 预检：必须是 InProgress 且我方未响应
        status, payload = self._request("GET", READY_CHECK_URL, None)
        if status != 200 or not isinstance(payload, dict):
            self._active = False
            return

        in_progress = payload.get("state") == "InProgress"
        unanswered = payload.get("playerResponse") == "None"
        if not in_progress or not unanswered:
            self._active = False
            return

        # 同一次 ready-check 不重复触发（重复执行保护）
        if self._active:
            return
        self._active = True
        ready_check_id = payload.get("readyCheckId")
        print("[AUTO_ACCEPT] ready check detected", flush=True)
        self._on_event("automation.ready_check_detected", {"ready_check_id": ready_check_id})

        # 2) 可中断等待（用 Event.wait，stop 时立刻返回，不等满）
        if accept_delay and self._stop.wait(accept_delay):
            print("[AUTO_ACCEPT] stopped during delay", flush=True)
            self._active = False
            return
        if self._stop.is_set():
            print("[AUTO_ACCEPT] stopped during delay", flush=True)
            self._active = False
            return

        # 3) 等待后复核：状态必须仍满足可 accept
        status2, current = self._request("GET", READY_CHECK_URL, None)
        if status2 != 200 or not isinstance(current, dict):
            self._active = False
            print("[AUTO_ACCEPT] verification failed: revalidate unreachable", flush=True)
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
            print("[AUTO_ACCEPT] verification failed: state changed", flush=True)
            self._on_event("automation.ready_check_expired", {"reason": "state_changed"})
            return

        # 4) POST accept
        print("[AUTO_ACCEPT] accepting", flush=True)
        accept_status, _ = self._request("POST", ACCEPT_URL, None)
        if accept_status not in (200, 204):
            self._active = False
            print(f"[AUTO_ACCEPT] POST failed status={accept_status}", flush=True)
            self._on_event(
                "automation.error",
                {"what": "accept_failed", "status": accept_status},
            )
            return

        self._on_event("automation.accept_sent", {"ready_check_id": ready_check_id})

        # 5) accept 后复核：应离开 InProgress（已接受 / 已消失）
        v3, after = self._request("GET", READY_CHECK_URL, None)
        if v3 == 200 and isinstance(after, dict):
            still_pending = (
                after.get("state") == "InProgress"
                and after.get("playerResponse") == "None"
            )
            if not still_pending:
                print("[AUTO_ACCEPT] verification passed", flush=True)
                self._on_event("automation.accept_verified",
                               {"ready_check_id": ready_check_id})
            else:
                print("[AUTO_ACCEPT] verification failed: still InProgress", flush=True)
                self._on_event(
                    "automation.error",
                    {"what": "accept_verify_failed", "status": accept_status},
                )
        else:
            # accept 已发送但无法复核；不判失败，仅记录
            print("[AUTO_ACCEPT] accept sent (post-verify unreachable)", flush=True)
        self._active = False

    def reset(self) -> None:
        self._active = False
