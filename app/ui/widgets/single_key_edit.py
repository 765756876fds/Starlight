"""SingleKeyEdit：基于官方 QKeySequenceEdit 的单键捕获控件。

点击控件后直接按键盘即可捕获并即时回显（无弹窗、无全局键盘钩子），
根除自建 TriggerCaptureDialog 弹窗"确认后不回填"的问题。
"""
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QKeySequenceEdit


class SingleKeyEdit(QKeySequenceEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaximumSequenceLength(1)
        self.keySequenceChanged.connect(self._on_changed)

    def _on_changed(self, seq: QKeySequence):
        if seq.count() > 1:
            self.setKeySequence(QKeySequence(seq[0]))

    def value(self) -> str:
        seq = self.keySequence()
        if seq.isEmpty():
            return ""
        return seq.toString().split(",")[0].strip()

    def set_value(self, key_str: str):
        if not key_str:
            self.clear()
        else:
            self.setKeySequence(QKeySequence(key_str))
