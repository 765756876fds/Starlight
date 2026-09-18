"""状态栏。"""
from PySide6.QtWidgets import QLabel, QStatusBar


class StatusBar(QStatusBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.lbl_lcu = QLabel("LCU: ○")
        self.lbl_asr = QLabel("ASR: ○")
        self.lbl_llm = QLabel("LLM: ○")
        self.addWidget(self.lbl_lcu)
        self.addWidget(self.lbl_asr)
        self.addWidget(self.lbl_llm)

    def set_lcu(self, ok: bool):
        self.lbl_lcu.setText("LCU: ●" if ok else "LCU: ○")

    def set_asr(self, ok: bool):
        self.lbl_asr.setText("ASR: ●" if ok else "ASR: ○")

    def set_llm(self, ok: bool):
        self.lbl_llm.setText("LLM: ●" if ok else "LLM: ○")
