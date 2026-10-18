from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout, QGridLayout, QComboBox, QHBoxLayout, QLabel, QPushButton,
)

from ...ui.theme.tokens import Tokens
from ...ui.widgets.glass_dialog import GlassDialog


class AutoQueueConfigDialog(GlassDialog):
    """自动排队配置：模式 + 主/副位置（GlassDialog 无边框 + QGridLayout 严格对齐）。"""

    MODES = [
        ("人机", "bot"),
        ("匹配", "normal"),
        ("极地大乱斗", "aram"),
        ("灵活排位", "flex"),
        ("单双排位", "solo_duo"),
    ]
    ROLES = ["不限", "上单", "打野", "中单", "ADC", "辅助"]

    def __init__(self, current: dict, parent=None):
        super().__init__("自动排队配置", parent)
        self.setMinimumWidth(360)
        T = Tokens

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(T.SPACING_SM)
        grid.setVerticalSpacing(T.SPACING_MD)   # 行距拉开（留白专业感）
        grid.setColumnStretch(1, 1)

        self.cb_mode = QComboBox()
        for label, val in self.MODES:
            self.cb_mode.addItem(label, userData=val)
        mode_val = current.get("auto_queue_mode", "normal")
        for i in range(self.cb_mode.count()):
            if self.cb_mode.itemData(i) == mode_val:
                self.cb_mode.setCurrentIndex(i)
                break

        self.cb_primary = QComboBox()
        self.cb_primary.addItems(self.ROLES)
        self.cb_primary.setCurrentText(current.get("auto_queue_primary_role", "不限"))

        self.cb_secondary = QComboBox()
        self.cb_secondary.addItems(self.ROLES)
        self.cb_secondary.setCurrentText(current.get("auto_queue_secondary_role", "不限"))

        def _label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setProperty("class", "secondary")
            return lbl

        grid.addWidget(_label("模式"), 0, 0)
        grid.addWidget(self.cb_mode, 0, 1)
        grid.addWidget(_label("主位置"), 1, 0)
        grid.addWidget(self.cb_primary, 1, 1)
        grid.addWidget(_label("副位置"), 2, 0)
        grid.addWidget(self.cb_secondary, 2, 1)

        # 底部按钮：统一靠右，间距 8px；主按钮 accent 实底、次按钮透明
        btns = QHBoxLayout()
        btns.setSpacing(T.SPACING_SM)
        btns.addStretch(1)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setProperty("class", "primary")
        self.btn_cancel = QPushButton("取消")
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_ok)
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

        lay = self.body_layout
        lay.addLayout(grid)
        lay.addSpacing(T.SPACING_MD)
        lay.addLayout(btns)

    def values(self) -> dict:
        return {
            "auto_queue_mode": self.cb_mode.currentData(),
            "auto_queue_primary_role": self.cb_primary.currentText(),
            "auto_queue_secondary_role": self.cb_secondary.currentText(),
        }
