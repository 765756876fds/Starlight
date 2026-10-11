"""Auto Queue：启动匹配前做硬性前置校验，启动后再做状态复核。

原则：先验证状态再执行；执行后再次验证。绝不定时器到了就盲 POST。
"""
from __future__ import annotations


SEARCH_URL = "/lol-lobby/v2/lobby/matchmaking/search"
SEARCH_STATE_URL = "/lol-lobby/v2/lobby/matchmaking/search-state"
LOBBY_URL = "/lol-lobby/v2/lobby"
POSITION_URL = "/lol-lobby/v1/lobby/members/localMember/position-preferences"

# 模式 -> queueId 映射
MODE_QUEUE_ID = {
    "bot": 325,        # 人机
    "normal": 430,     # 匹配
    "flex": 440,       # 灵活排位
    "solo_duo": 420,   # 单双排位
    "aram": 450,       # 极地大乱斗
}

# 位置中文 -> LCU位置代码
ROLE_MAP = {
    "不限": "Fill",
    "上单": "top",
    "打野": "jungle",
    "中单": "mid",
    "ADC": "bot",
    "辅助": "utility",
}

# 允许自动发起 matchmaking 的 gameflow 阶段
_ALLOWED_START_PHASES = ("Lobby",)
# 空闲阶段：自动创建房间
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

    def reset(self) -> None:
        self._search_posted = False
        self._prompted_user = False
        self._verified = False

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

        # 3) 仅当明确处于 Lobby 才发起开始匹配
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

        # 5) 空闲阶段：自动创建房间（新增功能，失败不影响原有逻辑）
        if gameflow_phase in _IDLE_PHASES:
            try:
                return self._auto_create_lobby(config)
            except Exception as e:
                print(f"[AUTO_QUEUE] auto create lobby failed: {e}", flush=True)
                # 创建失败就 fallback 到原来的提示
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

    def _auto_create_lobby(self, config) -> bool:
        """自动创建房间：选模式 + 选位置 + 创建房间。
        失败不抛异常，返回False，不影响原有逻辑。"""
        if config is None:
            return False

        mode = getattr(config, "auto_queue_mode", "normal")
        primary = getattr(config, "auto_queue_primary_role", "不限")
        secondary = getattr(config, "auto_queue_secondary_role", "不限")

        queue_id = MODE_QUEUE_ID.get(mode, 430)
        print(f"[AUTO_QUEUE] auto create lobby: mode={mode} queueId={queue_id}", flush=True)

        # 第一步：创建房间
        status, _ = self._request("POST", LOBBY_URL, {"queueId": queue_id})
        if status not in (200, 201, 204):
            print(f"[AUTO_QUEUE] create lobby failed: status={status}", flush=True)
            return False
        print("[AUTO_QUEUE] lobby created", flush=True)

        # 第二步：设置位置偏好（排位/匹配才需要，人机/极地不需要）
        if mode in ("solo_duo", "flex", "normal"):
            try:
                pos1 = ROLE_MAP.get(primary, "Fill")
                pos2 = ROLE_MAP.get(secondary, "Fill")
                if pos1 == "Fill" and pos2 == "Fill":
                    position_body = {"firstPreference": "Fill", "secondPreference": "Fill"}
                else:
                    position_body = {"firstPreference": pos1, "secondPreference": pos2}
                pos_status, _ = self._request("PUT", POSITION_URL, position_body)
                print(f"[AUTO_QUEUE] set position: {pos1}/{pos2} status={pos_status}", flush=True)
            except Exception as e:
                print(f"[AUTO_QUEUE] set position failed: {e}", flush=True)

        # 创建成功，下一个tick就会进入Lobby阶段，自动开始匹配
        return True
