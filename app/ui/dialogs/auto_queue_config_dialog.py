from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout, QFormLayout, QComboBox, QDialogButtonBox,
)

from ...ui.theme.tokens import Tokens
from ...ui.widgets.glass_dialog import GlassDialog


class AutoQueueConfigDialog(GlassDialog):
    """自动排队配置：模式 + 主/副位置（GlassDialog 无边框 + QFormLayout 标签右对齐）。"""

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

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setSpacing(Tokens.SPACING_SM)
        form.setContentsMargins(0, 0, 0, 0)

        self.cb_mode = QComboBox()
        for label, val in self.MODES:
            self.cb_mode.addItem(label, userData=val)
        mode_val = current.get("auto_queue_mode", "normal")
        for i in range(self.cb_mode.count()):
            if self.cb_mode.itemData(i) == mode_val:
                self.cb_mode.setCurrentIndex(i)
                break
        form.addRow("模式", self.cb_mode)

        self.cb_primary = QComboBox()
        self.cb_primary.addItems(self.ROLES)
        self.cb_primary.setCurrentText(current.get("auto_queue_primary_role", "不限"))
        form.addRow("主位置", self.cb_primary)

        self.cb_secondary = QComboBox()
        self.cb_secondary.addItems(self.ROLES)
        self.cb_secondary.setCurrentText(current.get("auto_queue_secondary_role", "不限"))
        form.addRow("副位置", self.cb_secondary)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        lay = self.body_layout
        lay.addLayout(form)
        lay.addWidget(btns)

    def values(self) -> dict:
        return {
            "auto_queue_mode": self.cb_mode.currentData(),
            "auto_queue_primary_role": self.cb_primary.currentText(),
            "auto_queue_secondary_role": self.cb_secondary.currentText(),
        }
