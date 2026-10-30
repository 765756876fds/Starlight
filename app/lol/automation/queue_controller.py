"""Auto Queue：启动匹配前做硬性前置校验，启动后再做状态复核。

原则：先验证状态再执行；执行后再次验证。绝不定时器到了就盲 POST。
"""
from __future__ import annotations

import time
import threading

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
_MAX_LOBBY_FAILURES = 5
_MAX_LOBBY_4XX_FAILURES = 3


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
        self._verified_in_queue = False  # 终态短路：已确认排队成功后不再发 search-state GET
        self._lobby_create_pending = False
        self._lobby_retry_at = 0.0
        self._lobby_failures = 0
        self._lobby_4xx_failures = 0
        self._lobby_state_lock = threading.RLock()
        self._lobby_generation = 0
        self._lobby_paused = False
        self._lobby_attempts = 0
        self._last_queue_probe = (False, False)

    def _queue_state(self) -> bool | None:
        status, payload = self._request("GET", SEARCH_STATE_URL, None)
        if status == 200 and isinstance(payload, dict):
            in_queue = bool(payload.get("isCurrentlyInQueue"))
            self._last_queue_probe = (True, in_queue)
            return in_queue
        self._last_queue_probe = (False, False)
        return None

    def is_currently_in_queue(self) -> bool:
        """兼容旧调用者：未知状态仍返回 False，但内部流程使用三态接口。"""
        return self._queue_state() is True

    def reset(self, *, clear_pause: bool = False) -> None:
        """清理临时请求状态；只有用户明确关闭时才清除失败暂停。"""
        with self._lobby_state_lock:
            self._lobby_generation += 1
            self._reset_state_locked()
            if clear_pause:
                self._lobby_paused = False
                self._lobby_failures = 0
                self._lobby_4xx_failures = 0

    def _reset_state_locked(self) -> None:
        self._search_posted = False
        self._prompted_user = False
        self._verified = False
        self._verified_in_queue = False
        self._lobby_create_pending = False
        self._lobby_retry_at = 0.0
        self._lobby_attempts = 0
        # _lobby_paused and failure counters deliberately survive internal reset.

    def _mark_verified(self) -> None:
        if not self._verified:
            self._verified = True
            self._search_posted = True
            self._verified_in_queue = True  # 终态短路：确认已排队后不再查 search-state
            self._on_event("automation.queue_search_verified", {})
            print("[AUTO_QUEUE] verification passed", flush=True)

    def is_lobby_paused(self) -> bool:
        with self._lobby_state_lock:
            return self._lobby_paused

    def try_start(self, gameflow_phase: str, config=None) -> bool:
        """True=本次发起了 POST search；False=无需动作 / 等待 / 已保护。"""
        # 0) 终态短路：已确认在队列中 → 直接返回，不再发 search-state GET
        if self._verified_in_queue:
            return False

        if gameflow_phase == "Lobby":
            self._lobby_create_pending = False

        # 1) 状态保护：已在队列中 → 绝不重复 POST
        queue_state = self._queue_state()
        if queue_state is None:
            # 查询失败不能等同于“不在队列”，禁止所有后续 POST。
            return False
        if queue_state:
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

        # 5) 空闲阶段：只等待用户手动进入 Lobby，不自动创建房间。
        if gameflow_phase in _IDLE_PHASES:
            return False

        # 6) 其他阶段（ReadyCheck/ChampSelect/InProgress/EndOfGame...）：
        #    复位，避免跨会话污染；不在这些阶段发起 queue
        self._search_posted = False
        self._prompted_user = False
        self._verified = False
        return False

    def _auto_create_lobby(self, config, gameflow_phase: str = "") -> bool:
        """自动创建房间：选模式 + 选位置 + 创建房间。
        失败不抛异常，返回False，不影响原有逻辑。"""
        if config is None:
            return False

        with self._lobby_state_lock:
            if (self._lobby_paused or self._lobby_create_pending
                    or time.monotonic() < self._lobby_retry_at):
                return False
            generation = self._lobby_generation
            self._lobby_attempts += 1
            attempt = self._lobby_attempts

        probe_ok, in_queue = self._last_queue_probe
        probe = "in_queue" if probe_ok and in_queue else (
            "not_in_queue" if probe_ok else "query_failed"
        )

        mode = getattr(config, "auto_queue_mode", "normal")
        primary = getattr(config, "auto_queue_primary_role", "不限")
        secondary = getattr(config, "auto_queue_secondary_role", "不限")

        queue_id = MODE_QUEUE_ID.get(mode, 430)
        print(
            f"[AUTO_QUEUE] create attempt={attempt} phase={gameflow_phase} "
            f"search_state={probe} mode={mode} queueId={queue_id}",
            flush=True,
        )

        # 第一步：创建房间
        status, body = self._request("POST", LOBBY_URL, {"queueId": queue_id})
        with self._lobby_state_lock:
            if generation != self._lobby_generation:
                print("[AUTO_QUEUE] stale lobby response ignored", flush=True)
                return False
        if status not in (200, 201, 204):
            with self._lobby_state_lock:
                if generation != self._lobby_generation:
                    print("[AUTO_QUEUE] stale lobby failure ignored", flush=True)
                    return False
                self._lobby_failures += 1
                failures = self._lobby_failures
                delay = min(60.0, 5.0 * (2 ** min(failures - 1, 3)))
                error_class = self._error_class(status)
                error_code = self._safe_error_code(body)
                # 当前实机阻断问题是持续 5xx；其他错误保留原有退避语义，
                # 避免把配置错误、断线或未知失败未经分类统一成永久暂停。
                if failures >= _MAX_LOBBY_FAILURES and error_class == "http_5xx":
                    self._lobby_paused = True
                    self._lobby_retry_at = 0.0
                elif error_class == "http_4xx":
                    self._lobby_4xx_failures += 1
                    if self._lobby_4xx_failures >= _MAX_LOBBY_4XX_FAILURES:
                        self._lobby_paused = True
                        self._lobby_retry_at = 0.0
                    else:
                        self._lobby_retry_at = time.monotonic() + delay
                else:
                    self._lobby_4xx_failures = 0
                    self._lobby_retry_at = time.monotonic() + delay
            code_text = error_code or "unclassified"
            if self._lobby_paused:
                pause_reason = (
                    "too_many_lobby_4xx_failures"
                    if error_class == "http_4xx"
                    else "too_many_lobby_failures"
                )
                pause_failures = (
                    self._lobby_4xx_failures
                    if error_class == "http_4xx"
                    else failures
                )
                print(
                    f"[AUTO_QUEUE] create lobby paused: attempts={attempt} "
                    f"failures={pause_failures} status={status} "
                    f"error={error_class} code={code_text} reason={pause_reason}",
                    flush=True,
                )
                self._on_event("automation.queue_paused", {
                    "reason": pause_reason,
                    "failures": pause_failures,
                })
            else:
                print(
                    f"[AUTO_QUEUE] create lobby failed: attempt={attempt} "
                    f"status={status} failures={failures} "
                    f"retry_in={delay:.0f}s error={error_class} code={code_text}",
                    flush=True,
                )
            return False
        print("[AUTO_QUEUE] lobby created", flush=True)
        with self._lobby_state_lock:
            if generation != self._lobby_generation:
                print("[AUTO_QUEUE] stale lobby success ignored", flush=True)
                return False
            self._lobby_create_pending = True
            self._lobby_retry_at = 0.0
            self._lobby_failures = 0
            self._lobby_4xx_failures = 0

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

    @staticmethod
    def _error_class(status: int) -> str:
        if status == 0:
            return "lcu_unavailable"
        if 500 <= status <= 599:
            return "http_5xx"
        if 400 <= status <= 499:
            return "http_4xx"
        return "http_error"

    @staticmethod
    def _safe_error_code(body) -> str | None:
        # 当前项目没有确认任何可安全记录的 LCU 业务错误码。
        # 保留接口以便未来加入经过审查的固定集合，但默认不输出服务端字段。
        return None
