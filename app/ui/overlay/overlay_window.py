"""OverlayWindow：Qt 透明无边框置顶鼠标穿透悬浮窗。"""
from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QLabel, QVBoxLayout, QWidget,
)

from .overlay_model import OverlayModel


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class OverlayWindow(QWidget):
    def __init__(self, model: OverlayModel, parent=None):
        super().__init__(parent)
        self._model = model
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(4)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

        self._place_default()
        self._apply_ws_ex_transparent()
        self.hide()

    def _apply_ws_ex_transparent(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            user32 = ctypes.windll.user32
            current = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                current | WS_EX_LAYERED | WS_EX_TRANSPARENT,
            )
        except Exception as e:
            print(f"[overlay] WS_EX_TRANSPARENT 设置失败: {e}")

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.setGeometry(screen.width() - 320, 80, 300, 400)

    def set_position(self, pos: str) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        w, h = 300, 400
        positions = {
            "top-right": (screen.width() - w - 20, 20),
            "top-left": (20, 20),
            "bottom-right": (screen.width() - w - 20, screen.height() - h - 60),
            "bottom-left": (20, screen.height() - h - 60),
        }
        x, y = positions.get(pos, positions["top-right"])
        self.setGeometry(x, y, w, h)

    def _refresh(self) -> None:
        try:
            items = self._model.get_items()
        except Exception:
            items = []
        # 清空
        while self._layout.count():
            it = self._layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        if not items:
            if self.isVisible():
                self.hide()
            return
        for it in items:
            label = QLabel(f"{it.text}   {_fmt(it.seconds_remaining)}")
            color = "#ff5252" if it.urgent else "#e0e0e0"
            label.setStyleSheet(
                f"color: {color}; background: rgba(30,30,30,180);"
                " padding: 4px 8px; border-radius: 4px;"
            )
            f = QFont("Segoe UI", 10, QFont.Bold)
            label.setFont(f)
            self._layout.addWidget(label)
        if not self.isVisible():
            self.show()
