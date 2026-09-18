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

        self.lbl_state = QLabel("state: ?")
        self.lbl_phase = QLabel("phase: ?")
        form.addWidget(self.lbl_phase)
        form.addWidget(self.lbl_state)

        self.btn_queue = ToggleButton("Auto Queue", False)
        self.btn_accept = ToggleButton("Auto Accept", False)
        self.btn_ban = ToggleButton("Auto Ban", False)
        self.btn_pick = ToggleButton("Auto Pick", False)

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
            self.lbl_state.setText(f"state: {state}")
            self.lbl_phase.setText(f"phase: {phase}")
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
