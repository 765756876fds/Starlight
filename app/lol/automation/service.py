"""LoLAutomationService：顶层入口。

订阅 EventBus（可选），启动 worker 线程周期性 polling LCU。
由 CommandBus 的 AUTO_ACCEPT_TOGGLE / AUTO_BANPICK_TOGGLE 切换开关。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ...core.event_bus import Event, EventBus, get_event_bus
from .action_executor import ChampSelectActionExecutor
from .champ_select_controller import ChampSelectController
from .models import AutomationConfig, AutomationState
from .queue_controller import QueueController
from .ready_check_controller import ReadyCheckController
from .state_machine import derive_state


class LoLAutomationService:
    def __init__(
        self,
        lcu,  # LCUConnection
        config: AutomationConfig | None = None,
        event_bus: EventBus | None = None,
    ):
        self._lcu = lcu
        self._config = config or AutomationConfig()
        self._bus = event_bus or get_event_bus()

        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._lock = threading.RLock()
        self._state = AutomationState.IDLE
        self._last_phase: str = "None"
        self._tick_hint = ""
        # Lobby 检测缓存（5s）：_is_unmanaged_flow 每 tick GET lobby 的空转来源
        self._last_lobby_check = 0.0
        self._cached_unmanaged = False
        # ChampSelect poll 日志降噪：仅首次进入选人阶段打一次
        self._champ_select_logged = False
        self._queue_config_warned = False

        # 事件桥
        def _request_with_status(method: str, endpoint: str, data):
            return self._lcu.request_raw(method, endpoint, data)

        self._on_event: Callable[[str, dict], None] = self._publish

        self._executor = ChampSelectActionExecutor(
            _request_with_status, self._stop_event, self._on_event
        )
        self._ready_check = ReadyCheckController(
            _request_with_status, self._stop_event, self._on_event
        )
        self._queue = QueueController(_request_with_status, self._on_event)
        self._champ_select = ChampSelectController(
            _request_with_status, self._executor, self._stop_event, self._on_event
        )

    def feature_status(self) -> dict:
        """开关状态直接取自 config（单一事实源）。
        真实执行路径由 self._queue/_ready_check/_champ_select 驱动。"""
        c = self._config
        return {
            "auto_queue": {"enabled": bool(c.auto_queue)},
            "auto_accept": {"enabled": bool(c.auto_accept)},
            "auto_ban": {"enabled": bool(c.auto_ban)},
            "auto_pick": {"enabled": bool(c.auto_pick)},
        }

    @staticmethod
    def print_feature_banner(cfg: AutomationConfig) -> None:
        print(f"[FEATURE] auto_queue     enabled={cfg.auto_queue}")
        print(f"[FEATURE] auto_accept    enabled={cfg.auto_accept}")
        print(f"[FEATURE] auto_ban       enabled={cfg.auto_ban}")
        print(f"[FEATURE] auto_pick      enabled={cfg.auto_pick}")
        print("[FEATURE] fallback       enabled=True")
        print("[FEATURE] verification   enabled=True")
        print("[FEATURE] safe_stop      enabled=True")

    # ---- config / toggles ----
    def update_config(self, cfg: AutomationConfig) -> None:
        with self._lock:
            self._config = cfg
    @property
    def config(self) -> AutomationConfig:
        with self._lock:
            return self._config

    def _ensure_worker_running(self) -> None:
        """如果 worker 没在跑，则启动。"""
        if not self.running:
            self._config.enabled = True
            self.start()

    def set_auto_accept(self, enabled: bool) -> None:
        with self._lock:
            self._config.auto_accept = bool(enabled)
        if enabled:
            self._ensure_worker_running()

    def set_auto_queue(self, enabled: bool) -> None:
        with self._lock:
            self._config.auto_queue = bool(enabled)
            if enabled:
                self._queue_config_warned = False
        if enabled:
            self._ensure_worker_running()

    def set_auto_banpick(self, enabled: bool) -> None:
        with self._lock:
            self._config.auto_ban = bool(enabled)
            self._config.auto_pick = bool(enabled)
        if enabled:
            self._ensure_worker_running()

    def toggle_auto_accept(self) -> bool:
        with self._lock:
            new_val = not self._config.auto_accept
            self._config.auto_accept = new_val
        self._ensure_worker_running()
        return new_val

    def toggle_auto_queue(self) -> bool:
        self.set_auto_queue(not self.config.auto_queue)
        return self.config.auto_queue

    def set_auto_ban(self, enabled: bool) -> None:
        with self._lock:
            self._config.auto_ban = bool(enabled)
        if enabled:
            self._ensure_worker_running()

    def set_auto_pick(self, enabled: bool) -> None:
        with self._lock:
            self._config.auto_pick = bool(enabled)
        if enabled:
            self._ensure_worker_running()

    def toggle_auto_ban(self) -> bool:
        self.set_auto_ban(not self.config.auto_ban)
        return self.config.auto_ban

    def toggle_auto_pick(self) -> bool:
        self.set_auto_pick(not self.config.auto_pick)
        return self.config.auto_pick

    def toggle_auto_banpick(self) -> bool:
        # 兼容旧入口：同时翻转 ban 和 pick
        with self._lock:
            new_val = not (self._config.auto_ban or self._config.auto_pick)
            self._config.auto_ban = new_val
            self._config.auto_pick = new_val
        self._ensure_worker_running()
        return new_val

    # ---- lifecycle ----
    def start(self) -> bool:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return False
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._run_loop, name="lol-automation", daemon=True
            )
        self._publish("automation.started", {})
        self._worker.start()
        return True

    def stop(self, timeout: float = 6.0) -> bool:
        with self._lock:
            thread = self._worker
        self._stop_event.set()
        self._ready_check.reset()
        self._champ_select.clear_state()
        self._set_state(AutomationState.SAFE_STOP)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        alive = bool(thread and thread.is_alive())
        if not alive:
            self._publish("automation.stopped", {})
        return not alive

    @property
    def running(self) -> bool:
        return bool(self._worker and self._worker.is_alive() and not self._stop_event.is_set())

    # ---- main loop ----
    def _set_state(self, s: AutomationState) -> None:
        if s != self._state:
            self._state = s
            self._publish("automation.state_changed", {"state": s.value})

    def _publish(self, code: str, data: dict) -> None:
        try:
            self._bus.publish(Event(event_type=code, data=data, source="lol-automation"))
        except Exception:
            pass

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self._tick()
                except Exception as e:
                    self._publish("automation.error", {"what": "tick", "error": str(e)})
                self._stop_event.wait(self.config.polling_interval)
        finally:
            self._champ_select.clear_state()
            self._ready_check.reset()
            self._set_state(AutomationState.IDLE)

    def _tick(self) -> None:
        cfg = self.config
        if not cfg.enabled:
            return
        # 无任何功能开启：空转不做事不打印（关闭路径只改 flag 不停 worker，
        # 此处是"全关"的静默出口，避免 ChampSelect 阶段每秒刷 poll 日志；
        # 全关时 poll 根本到不了这里之后的分支，404 双保险无 pending 可清，无副作用）
        if not (cfg.auto_queue or cfg.auto_accept or cfg.auto_ban or cfg.auto_pick):
            return

        if not self._lcu.connected:
            self._set_state(AutomationState.CLIENT_DISCONNECTED)
            return

        phase = self._lcu.get_gameflow_phase() or "None"

        # 离开 ChampSelect（正常进游戏 / 结算）主动清 state，避免跨 session 污染。
        # 不依赖 404；404 路径在 poll 内仍保留作为双保险。
        if self._last_phase == "ChampSelect" and phase != "ChampSelect":
            self._champ_select.clear_state()
            self._champ_select_logged = False  # 离开选人阶段：允许下次进入时重新打一次日志
        self._last_phase = phase

        # 训练模式 / 自定义房间：Agent 完全不接管（不排队、不 accept、不选人）。
        # 这是用户实测 Bug 的根因：practice 出现 ChampSelect 被无差别接管，
        # 退出后下一 tick 又因 phase==ChampSelect/Lobby 被重新推进。
        if self._is_unmanaged_flow():
            self._champ_select.clear_state()
            self._queue.reset()
            self._tick_hint = "训练/自定义房间：Automation 已忽略"
            self._set_state(AutomationState.IDLE)
            return

        hints = []
        if cfg.auto_queue:
            if not cfg.queue_configured:
                if not self._queue_config_warned:
                    self._queue_config_warned = True
                    self._publish("automation.queue_not_configured", {})
            elif phase == "Lobby":
                hints.append("将自动开始匹配")
            elif phase == "Matchmaking":
                hints.append("已在排队中")
            elif cfg.queue_configured:
                hints.append("等待进入组队房间")
        if cfg.auto_accept:
            if phase == "ReadyCheck":
                hints.append("将自动接受")
            else:
                hints.append("等待 ReadyCheck")
        if cfg.auto_ban or cfg.auto_pick:
            if phase == "ChampSelect":
                hints.append("将在选人阶段自动操作")
            else:
                hints.append("等待进入选人阶段")
        self._tick_hint = " | ".join(hints) if hints else "空闲"

        # 状态推导（双信号：gameflow + 动作本身）
        self._set_state(derive_state(phase))
        # ReadyCheck 阶段只做 accept；其余阶段做 queue 与 champ_select。
        # 这样 auto_queue 与 auto_accept 可同时开启，不再被 elif 饿死。
        if phase == "ReadyCheck":
            if cfg.auto_accept:
                self._log("auto_accept", "ReadyCheck detected -> four-step check")
                self._ready_check.check(cfg.accept_delay)
        else:
            if cfg.auto_queue and cfg.queue_configured:
                self._queue.try_start(phase, cfg)
            if phase == "ChampSelect":
                # 日志降噪：仅首次进入选人阶段打一次（离开时复位），不再每 tick 刷屏
                if cfg.auto_ban or cfg.auto_pick:
                    if not getattr(self, "_champ_select_logged", False):
                        self._log("champ_select", f"ChampSelect poll active (ban={cfg.auto_ban} pick={cfg.auto_pick})")
                        self._champ_select_logged = True
                self._champ_select.poll(cfg)

    def get_status_hint(self) -> str:
        return getattr(self, "_tick_hint", "")

    def _is_unmanaged_flow(self) -> bool:
        """True=当前不是 Agent 应接管的匹配流程（训练模式/自定义/人机练习房）。

        依据 LCU /lol-lobby/v2/lobby 的 gameConfig：gameMode=PRACTICETOOL 或
        isCustom=True。读取失败时保守返回 False（不误伤正常排位）。
        结果 5 秒缓存：避免每 tick 无条件 GET lobby 的空转请求。
        """
        now = time.time()
        if now - getattr(self, "_last_lobby_check", 0.0) < 5.0:
            return getattr(self, "_cached_unmanaged", False)
        result = False
        try:
            status, lobby = self._request_with_status("GET", "/lol-lobby/v2/lobby", None)
            if status == 200 and isinstance(lobby, dict):
                gc = lobby.get("gameConfig") or {}
                if gc.get("gameMode") == "PRACTICETOOL" or gc.get("isCustom"):
                    result = True
        except Exception:
            result = False
        self._last_lobby_check = now
        self._cached_unmanaged = result
        return result

    @staticmethod
    def _log(feature: str, msg: str) -> None:
        print(f"[{feature}] {msg}", flush=True)
