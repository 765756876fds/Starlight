"""OverlayWindow：Qt 透明无边框置顶（鼠标穿透）浮窗（敌方召唤师技能 CD）。

窗口属性沿用现有实现（frameless / always-on-top / transparent / click-through），
只按 OverlayModel.get_rows() 渲染：每行 = Champion + 两个召唤师技能槽。
UI 100ms presentation 刷新，不轮询 2999。
"""
from __future__ import annotations

import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from .overlay_model import OverlayModel, OverlaySpell, fmt_remaining


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
        self.setGeometry(screen.width() - 280, 80, 260, 420)

    def _clear(self):
        while self._root.count():
            it = self._root.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _spell_cell(self, sp: OverlaySpell) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        if sp.active:
            txt = fmt_remaining(sp.remaining)
            urgent = 0 <= sp.remaining < 10
            color = "#ff5252" if urgent else "#ffffff"
            lbl = QLabel(txt)
            lbl.setStyleSheet(
                f"color:{color}; background:rgba(60,30,60,210); padding:2px 6px; border-radius:3px;")
            lbl.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        else:
            lbl = QLabel(sp.spell_name or "—")
            lbl.setStyleSheet("color:#8a8a8a; padding:2px 6px;")
            lbl.setFont(QFont("Microsoft YaHei", 8))
        v.addWidget(lbl)
        return w

    def _skill_cell(self, sk) -> QWidget:
        """显示 Q/W/E/R 技能冷却。"""
        from .overlay_model import OverlaySkill
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        if sk.active:
            txt = fmt_remaining(sk.remaining)
            urgent = 0 <= sk.remaining < 10
            color = "#ff9800" if urgent else "#ffcc80"  # 橙色系区分召唤师技能
            lbl = QLabel(txt)
            lbl.setStyleSheet(
                f"color:{color}; background:rgba(60,40,20,210); padding:2px 6px; border-radius:3px;")
            lbl.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        else:
            lbl = QLabel("Ready")
            lbl.setStyleSheet("color:#4caf50; padding:2px 6px;")
            lbl.setFont(QFont("Microsoft YaHei", 8))
        v.addWidget(lbl)
        return w

    def _refresh(self) -> None:
        try:
            rows = self._model.get_rows()
        except Exception:
            import traceback; traceback.print_exc()
            rows = []
        self._clear()
        if not rows:
            if self.isVisible():
                self.hide()
            return
        for row in rows:
            rw = QWidget()
            h = QHBoxLayout(rw)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            champ = QLabel(row.champion_name)
            champ.setStyleSheet("color:#fff; background:rgba(20,20,20,180); padding:2px 6px; border-radius:3px;")
            champ.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
            h.addWidget(champ)
            # Q/W/E/R
            h.addWidget(self._skill_cell(row.skill_q))
            h.addWidget(self._skill_cell(row.skill_w))
            h.addWidget(self._skill_cell(row.skill_e))
            h.addWidget(self._skill_cell(row.skill_r))
            # 召唤师技能
            h.addWidget(self._spell_cell(row.spell1))
            h.addWidget(self._spell_cell(row.spell2))
            self._root.addWidget(rw)
        if not self.isVisible():
            self.show()
