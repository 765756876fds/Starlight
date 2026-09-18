"""Auto Queue：启动匹配前做硬性前置校验，启动后再做状态复核。

原则：先验证状态再执行；执行后再次验证。绝不定时器到了就盲 POST。
"""
from __future__ import annotations


SEARCH_URL = "/lol-lobby/v2/lobby/matchmaking/search"
SEARCH_STATE_URL = "/lol-lobby/v2/lobby/matchmaking/search-state"

# 允许自动发起 matchmaking 的 gameflow 阶段
_ALLOWED_START_PHASES = ("Lobby",)
# 空闲阶段：提示用户手动建房
_IDLE_PHASES = ("None", "LobbyClient", "")


class QueueController:
    def __init__(
        self,
        request_with_status,
        on_event,
    ):
        self._request = request_with_status
        self._on_event = on_event
        self._search_posted = False
        self._prompted_user = False
        self._verified = False

    def is_currently_in_queue(self) -> bool:
        status, payload = self._request("GET", SEARCH_STATE_URL, None)
        if status == 200 and isinstance(payload, dict):
            return bool(payload.get("isCurrentlyInQueue"))
        return False

    def _mark_verified(self) -> None:
        if not self._verified:
            self._verified = True
            self._search_posted = True
            self._on_event("automation.queue_search_verified", {})
            print("[AUTO_QUEUE] verification passed", flush=True)

    def try_start(self, gameflow_phase: str, config=None) -> bool:
        """True=本次发起了 POST search；False=无需动作 / 等待 / 已保护。"""
        # 1) 状态保护：已在队列中 → 绝不重复 POST
        if self.is_currently_in_queue():
            self._mark_verified()
            return False

        # 2) 已经在匹配中 → 不重复发起
        if gameflow_phase == "Matchmaking":
            return False

        # 3) 仅当明确处于 Lobby 才发起
        if gameflow_phase in _ALLOWED_START_PHASES:
            if self._search_posted:
                # 已发起但尚未在状态里确认（复核中），不重复 POST
                return False
            print(f"[AUTO_QUEUE] state={gameflow_phase}, starting matchmaking", flush=True)
            status, _ = self._request("POST", SEARCH_URL, None)
            if status in (200, 204):
                self._search_posted = True
                print("[AUTO_QUEUE] matchmaking request success", flush=True)
                self._on_event("automation.queue_search_started", {})
                # 4) 执行后复核：重新读取 search-state，确认真的进了队列
                if self.is_currently_in_queue():
                    self._mark_verified()
                else:
                    print("[AUTO_QUEUE] verification failed: not in queue after POST", flush=True)
                    self._on_event("automation.queue_search_verify_failed",
                                   {"reason": "not_in_queue_after_post"})
            else:
                print(f"[AUTO_QUEUE] POST failed status={status}", flush=True)
                self._on_event("automation.error",
                               {"what": "search_failed", "status": status})
            return True

        # 5) 空闲：提示手动建房（仅提示一次）
        if gameflow_phase in _IDLE_PHASES:
            if not self._prompted_user:
                self._prompted_user = True
                self._on_event("automation.needs_lobby", {
                    "hint": "请点击 Play → 选择模式 → 创建房间，程序将自动排队"
                })
                print("[AUTO_QUEUE] idle: please create a lobby manually", flush=True)
            return False

        # 6) 其他阶段（ReadyCheck/ChampSelect/InProgress/EndOfGame...）：
        #    复位，避免跨会话污染；不在这些阶段发起 queue
        self._search_posted = False
        self._prompted_user = False
        self._verified = False
        return False
