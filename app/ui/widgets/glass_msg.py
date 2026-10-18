"""Glass 消息框 / 确认框：替换原生 QMessageBox。

消灭原生白色标题栏（FramelessWindowHint + WA_TranslucentBackground）、
统一中文按钮（确定 / 取消 / 是 / 否）、主操作按钮走 primary 蓝实底 QSS。
用法（与 QMessageBox 静态方法同构）：
    glass_info(parent, "标题", "内容")
    ok = glass_question(parent, "标题", "确定继续？")   # 返回 bool
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton,
)

from .glass_dialog import GlassDialog
from ..theme.colors import Colors
from ..theme.tokens import Tokens


class _GlassMsgBox(GlassDialog):
    """通用消息框：文本 + 底部按钮行（右对齐；primary 高亮主操作）。"""

    def __init__(self, parent: Optional[object], title: str, text: str,
                 buttons: list[tuple[str, bool]], danger_yes: bool = False):
        super().__init__(title, parent)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(
            lbl.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: {Tokens.FONT_SIZE_BODY}px;")
        self.body_layout.addWidget(lbl)

        row = QHBoxLayout()
        row.setSpacing(Tokens.SPACING_SM)
        row.addStretch()
        self._result: Optional[str] = None
        for text_btn, primary in buttons:
            b = QPushButton(text_btn)
            b.setMinimumWidth(88)
            if primary:
                b.setProperty("class", "danger" if danger_yes else "primary")
            b.clicked.connect(lambda _=False, t=text_btn: self._finish(t))
            row.addWidget(b)
        self.body_layout.addLayout(row)

    def _finish(self, text: str):
        self._result = text
        self.accept()


def glass_info(parent, title: str, text: str) -> None:
    _GlassMsgBox(parent, title, text, [("确定", True)]).exec()


def glass_warning(parent, title: str, text: str) -> None:
    _GlassMsgBox(parent, title, text, [("确定", True)]).exec()


def glass_critical(parent, title: str, text: str) -> None:
    _GlassMsgBox(parent, title, text, [("确定", True)]).exec()


def glass_question(parent, title: str, text: str) -> bool:
    """确认框：取消（透明）+ 是（primary 蓝实底）。返回是否点了「是」。"""
    dlg = _GlassMsgBox(parent, title, text, [("取消", False), ("是", True)])
    dlg.exec()
    return dlg._result == "是"


def glass_confirm_danger(parent, title: str, text: str,
                         yes_text: str = "确定删除") -> bool:
    """危险操作确认框：取消（透明）+ 主操作（danger 红实底）。返回是否点了主操作。"""
    dlg = _GlassMsgBox(parent, title, text, [("取消", False), (yes_text, True)],
                       danger_yes=True)
    dlg.exec()
    return dlg._result == yes_text
