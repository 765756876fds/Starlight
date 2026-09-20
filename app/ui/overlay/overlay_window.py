"""OverlayWindow：Qt 透明无边框置顶鼠标穿透浮窗（敌方召唤师技能 CD）。"""
from __future__ import annotations

import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from .overlay_model import OverlayModel


def _fmt(sec: float) -> str:
    """Akari 式：>=10s 整数 mm:ss，<10s 一位小数，<=0 显 OK。"""
    if sec <= 0:
        return "OK"
    if sec < 10:
        return f"{sec:.1f}"
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


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

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(8, 8, 8, 8)
        self._root.setSpacing(4)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(100)  # UI 100ms 刷新，不触发 2999 轮询

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
            cur = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, cur | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        except Exception as e:
            print(f"[overlay] WS_EX_TRANSPARENT 失败: {e}")

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.setGeometry(screen.width() - 260, 80, 240, 360)

    def _clear(self):
        while self._root.count():
            it = self._root.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _refresh(self) -> None:
        try:
            entries = self._model.get_entries()
        except Exception:
            entries = []
        self._clear()
        if not entries:
            if self.isVisible():
                self.hide()
            return
        for e in entries:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            champ = QLabel(e.champion_name)
            champ.setStyleSheet("color:#fff; background:rgba(20,20,20,180); padding:2px 6px; border-radius:3px;")
            champ.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
            h.addWidget(champ)
            for s in e.skills:
                lbl = QLabel(_fmt(s.seconds_remaining))
                urgent = s.seconds_remaining < 30
                col = "#ff5252" if urgent else "#e0e0e0"
                lbl.setStyleSheet(
                    f"color:{col}; background:rgba(60,30,60,200); padding:2px 6px; border-radius:3px;")
                lbl.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
                lbl.setToolTip(s.skill)
                h.addWidget(lbl)
            self._root.addWidget(row)
        if not self.isVisible():
            self.show()
