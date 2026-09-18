"""自动化面板：4 个开关 + 状态显示。"""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QLabel, QVBoxLayout, QWidget,
)

from ..widgets.toggle_button import ToggleButton


class AutomationPanel(QWidget):
    def __init__(self, service=None, parent=None):
        super().__init__(parent)
        self._svc = service
        layout = QVBoxLayout(self)

        box = QGroupBox("LoL 自动化")
        form = QVBoxLayout(box)

        self.lbl_state = QLabel("状态：?")
        self.lbl_phase = QLabel("游戏阶段：?")
        form.addWidget(self.lbl_phase)
        form.addWidget(self.lbl_state)

        self.btn_queue = ToggleButton("自动排队", False)
        self.btn_accept = ToggleButton("自动接受", False)
        self.btn_ban = ToggleButton("自动禁用", False)
        self.btn_pick = ToggleButton("自动选择", False)

        for b in (self.btn_queue, self.btn_accept, self.btn_ban, self.btn_pick):
            form.addWidget(b)

        layout.addWidget(box)
        layout.addStretch()

        # 绑定回调
        if service is not None:
            self.btn_queue.toggled_state.connect(
                lambda on: _safe(service, "set_auto_queue", on))
            self.btn_accept.toggled_state.connect(
                lambda on: _safe(service, "set_auto_accept", on))
            self.btn_ban.toggled_state.connect(
                lambda on: _safe(service, "set_auto_ban", on))
            self.btn_pick.toggled_state.connect(
                lambda on: _safe(service, "set_auto_pick", on))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    def refresh(self):
        if self._svc is None:
            return
        try:
            state = self._svc._state.value if hasattr(self._svc, "_state") else "?"
            phase = getattr(self._svc, "_last_phase", "?")
            _STATE_ZH = {"idle":"空闲","in_lobby":"在大厅","queue_searching":"排队中",
                         "ready_check":"确认对局","champ_select":"英雄选择","in_game":"游戏中",
                         "settlement":"结算中","safe_stop":"已停止"}
            state_zh = _STATE_ZH.get(state, state)
            self.lbl_state.setText(f"状态：{state_zh}")
            self.lbl_phase.setText(f"游戏阶段：{phase}")
            cfg = getattr(self._svc, "config", None)
            if cfg is not None:
                self.btn_queue.set_state(bool(getattr(cfg, "auto_queue", False)))
                self.btn_accept.set_state(bool(getattr(cfg, "auto_accept", False)))
                self.btn_ban.set_state(bool(getattr(cfg, "auto_ban", False)))
                self.btn_pick.set_state(bool(getattr(cfg, "auto_pick", False)))
        except Exception:
            pass


def _safe(obj, method, *args):
    fn = getattr(obj, method, None)
    if fn is not None:
        try:
            fn(*args)
        except Exception:
            pass
