"""统一样式开关按钮。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QPushButton


class ToggleButton(QPushButton):
    """ON/OFF 按钮。"""

    toggled_state = Signal(bool)

    def __init__(self, label: str, initial: bool = False, parent=None):
        super().__init__(parent)
        self._label = label
        self._state = initial
        self.setCheckable(True)
        self.setChecked(initial)
        self.setFixedHeight(32)
        self.clicked.connect(self._on_click)
        self._refresh()

    def _on_click(self):
        self._state = self.isChecked()
        self._refresh()
        self.toggled_state.emit(self._state)

    def set_state(self, on: bool):
        self._state = bool(on)
        self.blockSignals(True)
        self.setChecked(self._state)
        self.blockSignals(False)
        self._refresh()

    def state(self) -> bool:
        return self._state

    def _refresh(self):
        self.setText(f"{self._label}: {'ON' if self._state else 'OFF'}")
        style = "background-color: #2e7d32; color: white;" if self._state \
            else "background-color: #555; color: white;"
        self.setStyleSheet(style + " border-radius: 6px; padding: 4px 12px;")
