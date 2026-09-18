"""统一样式开关按钮（深色主题，通过 QSS property 控制）。"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPushButton


class ToggleButton(QPushButton):
    toggled_state = Signal(bool)

    def __init__(self, label: str, initial: bool = False, parent=None):
        super().__init__(parent)
        self._label = label
        self._on = bool(initial)
        self.setCheckable(True)
        self.setChecked(self._on)
        self.setProperty("toggle", "on" if self._on else "off")
        self.setMinimumWidth(140)
        self.setMinimumHeight(36)
        self.clicked.connect(self._on_click)
        self._refresh()

    def _on_click(self):
        self._on = self.isChecked()
        self.setProperty("toggle", "on" if self._on else "off")
        self._refresh()
        self._repolish()
        self.toggled_state.emit(self._on)

    def set_state(self, on: bool):
        self._on = bool(on)
        self.blockSignals(True)
        self.setChecked(self._on)
        self.blockSignals(False)
        self.setProperty("toggle", "on" if self._on else "off")
        self._refresh()
        self._repolish()

    def state(self) -> bool:
        return self._on

    def _refresh(self):
        self.setText(f"{self._label}：{'开' if self._on else '关'}")

    def _repolish(self):
        self.style().unpolish(self)
        self.style().polish(self)
