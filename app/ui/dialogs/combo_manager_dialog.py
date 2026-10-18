"""连招管理弹窗：把 ComboPanel 原样装入 GlassDialog（非模态，关闭即销毁）。

ComboPanel 内部逻辑一行不改——只换容器。combo_manager 由 AutomationPanel 传入。
"""
from ..panels.combo_panel import ComboPanel
from ..widgets.glass_dialog import GlassDialog


class ComboManagerDialog(GlassDialog):
    def __init__(self, combo_manager, parent=None):
        super().__init__("管理连招", parent)
        self.resize(760, 560)
        if combo_manager is not None:
            self.body_layout.addWidget(ComboPanel(combo_manager))
