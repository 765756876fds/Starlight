"""连招管理弹窗：把 ComboPanel 原样装入 QDialog（非模态，关闭即销毁）。

ComboPanel 内部逻辑一行不改——只换容器。combo_manager 由 AutomationPanel 传入。
"""
from PySide6.QtWidgets import QDialog, QVBoxLayout

from ..panels.combo_panel import ComboPanel


class ComboManagerDialog(QDialog):
    def __init__(self, combo_manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("管理连招")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        if combo_manager is not None:
            lay.addWidget(ComboPanel(combo_manager))
