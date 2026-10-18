"""延迟接受配置对话框（替换原生 QInputDialog.getDouble）。

GlassDialog 无边框深色圆角；中文 确定/取消 按钮；确定走 primary 蓝实底；
数字输入框打开时光标置于末尾、不全选高亮。
"""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton

from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.glass_dialog import GlassDialog


class DelayConfigDialog(GlassDialog):
    def __init__(self, current: float = 0.0, parent=None):
        super().__init__("延迟接受配置", parent)

        hint = QLabel("延迟接受（秒，0=立即接受）：")
        hint.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_BODY}px;")
        self.body_layout.addWidget(hint)

        self._spin = QDoubleSpinBox()
        self._spin.setRange(0.0, 30.0)
        self._spin.setDecimals(1)
        self._spin.setSingleStep(0.1)
        self._spin.setValue(float(current or 0.0))
        self._spin.setMinimumWidth(140)
        self.body_layout.addWidget(self._spin)

        row = QHBoxLayout()
        row.setSpacing(Tokens.SPACING_SM)
        row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.setMinimumWidth(88)
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("确定")
        btn_ok.setProperty("class", "primary")
        btn_ok.setMinimumWidth(88)
        btn_ok.clicked.connect(self.accept)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        self.body_layout.addLayout(row)

        # 打开时 0.0 默认全选高亮 → 光标放末尾、取消选中
        QTimer.singleShot(0, self._place_cursor)

    def _place_cursor(self):
        le = self._spin.lineEdit()
        le.deselect()
        le.setCursorPosition(len(le.text()))

    def value(self) -> float:
        return self._spin.value()
