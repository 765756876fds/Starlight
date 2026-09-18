"""LoLAutomationService：顶层入口。

订阅 EventBus（可选），启动 worker 线程周期性 polling LCU。
由 CommandBus 的 AUTO_ACCEPT_TOGGLE / AUTO_BANPICK_TOGGLE 切换开关。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from ...core.event_bus import Event, EventBus, get_event_bus
from .action_executor import ChampSelectActionExecutor
from .champ_select_controller import ChampSelectController
from .features.auto_accept import AutoAcceptFeature
from .features.auto_ban import AutoBanFeature, AutoPickFeature
from .features.auto_queue import AutoQueueFeature
from .features.fallback import FallbackFeature
from .features.safe_stop import SafeStopFeature
from .features.verification import VerificationFeature
from .models import AutomationConfig, AutomationState
from .queue_controller import QueueController
from .ready_check_controller import ReadyCheckController
from .state_machine import derive_state


class LoLAutomationService:
    def __init__(
        self,
        lcu,  # LCUConnection
        config: Optional[AutomationConfig] = None,
        event_bus: Optional[EventBus] = None,
    ):
        self._lcu = lcu
        self._config = config or AutomationConfig()
        self._bus = event_bus or get_event_bus()

        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._state = AutomationState.IDLE
        self._last_phase: str = "None"

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

        # --- feature 注册（独立可观察）---
        self._features = {
            "auto_queue": AutoQueueFeature(_request_with_status, self._on_event, enabled=False),
            "auto_accept": AutoAcceptFeature(
                _request_with_status, self._stop_event, self._on_event,
                accept_delay=self._config.accept_delay, enabled=False,
            ),
            "auto_ban": AutoBanFeature(self._champ_select, enabled=False),
            "auto_pick": AutoPickFeature(self._champ_select, enabled=False),
            "fallback": FallbackFeature(enabled=True),
            "verification": VerificationFeature(enabled=True),
            "safe_stop": SafeStopFeature(self._stop_event, enabled=True),
        }
        self._sync_feature_switches()

    def _sync_feature_switches(self) -> None:
        """根据当前 config 同步各 feature 的 enabled。"""
        cfg = self._config
        self._features["auto_queue"].enabled = bool(cfg.auto_queue)
        self._features["auto_accept"].enabled = bool(cfg.auto_accept)
        self._features["auto_ban"].enabled = bool(cfg.auto_ban)
        self._features["auto_pick"].enabled = bool(cfg.auto_pick)

    def feature_status(self) -> dict:
        return {name: f.status() for name, f in self._features.items()}

    @staticmethod
    def print_feature_banner(cfg: "AutomationConfig") -> None:
        print(f"[FEATURE] auto_queue     enabled={cfg.auto_queue}")
        print(f"[FEATURE] auto_accept    enabled={cfg.auto_accept}")
        print(f"[FEATURE] auto_ban       enabled={cfg.auto_ban}")
        print(f"[FEATURE] auto_pick      enabled={cfg.auto_pick}")
        print(f"[FEATURE] fallback       enabled=True")
        print(f"[FEATURE] verification   enabled=True")
        print(f"[FEATURE] safe_stop      enabled=True")

    # ---- config / toggles ----
    def update_config(self, cfg: AutomationConfig) -> None:
        with self._lock:
            self._config = cfg
            self._sync_feature_switches()

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
            self._sync_feature_switches()
        if enabled:
            self._ensure_worker_running()

    def set_auto_banpick(self, enabled: bool) -> None:
        with self._lock:
            self._config.auto_ban = bool(enabled)
            self._config.auto_pick = bool(enabled)
            self._sync_feature_switches()
        if enabled:
            self._ensure_worker_running()

    def toggle_auto_accept(self) -> bool:
        with self._lock:
            new_val = not self._config.auto_accept
            self._config.auto_accept = new_val
            self._sync_feature_switches()
        self._ensure_worker_running()
        return new_val

    def set_auto_ban(self, enabled: bool) -> None:
        with self._lock:
            self._config.auto_ban = bool(enabled)
            self._sync_feature_switches()
        if enabled:
            self._ensure_worker_running()

    def set_auto_pick(self, enabled: bool) -> None:
        with self._lock:
            self._config.auto_pick = bool(enabled)
            self._sync_feature_switches()
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
            self._sync_feature_switches()
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

        if not self._lcu.connected:
            self._set_state(AutomationState.CLIENT_DISCONNECTED)
            return

        phase = self._lcu.get_gameflow_phase() or "None"

        # 离开 ChampSelect（正常进游戏 / 结算）主动清 state，避免跨 session 污染。
        # 不依赖 404；404 路径在 poll 内仍保留作为双保险。
        if self._last_phase == "ChampSelect" and phase != "ChampSelect":
            self._champ_select.clear_state()
        self._last_phase = phase

        # 状态推导（双信号：gameflow + 动作本身）
        self._set_state(derive_state(phase))
        if phase == "Lobby" and cfg.auto_queue:
            self._log("auto_queue", "Lobby, try_start -> POST search")
            self._queue.try_start(phase)
        elif phase == "ReadyCheck" and cfg.auto_accept:
            self._log("auto_accept", "ReadyCheck detected -> four-step check")
            self._ready_check.check(cfg.accept_delay)
        elif phase == "ChampSelect":
            self._log("champ_select", f"ChampSelect poll (ban={cfg.auto_ban} pick={cfg.auto_pick})")
            self._champ_select.poll(cfg)

    @staticmethod
    def _log(feature: str, msg: str) -> None:
        print(f"[{feature}] {msg}", flush=True)
