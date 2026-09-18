"""状态栏：实时读服务状态。"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QStatusBar


def _dot(ok: bool) -> str:
    color = "#4ade80" if ok else "#ef4444"
    return f'<span style="color:{color};font-size:14px;">●</span>'


class StatusBar(QStatusBar):
    def __init__(self, services=None, parent=None):
        super().__init__(parent)
        self._svc = services or {}
        self.lbl_lcu = QLabel()
        self.lbl_asr = QLabel()
        self.lbl_llm = QLabel()
        for lbl in (self.lbl_lcu, self.lbl_asr, self.lbl_llm):
            lbl.setTextFormat(Qt.RichText)
            lbl.setStyleSheet("padding: 0 8px;")
            self.addWidget(lbl)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self.refresh()

    def refresh(self):
        lcu = self._svc.get("lcu")
        lcu_ok = False
        if lcu is not None:
            lcu_ok = bool(getattr(lcu, "connected", False))
        self.lbl_lcu.setText(f"{_dot(lcu_ok)} LCU {'已连接' if lcu_ok else '未连接'}")

        asr = self._svc.get("asr")
        asr_label = "未就绪"
        asr_ok = False
        if asr is not None:
            engines = getattr(asr, "engines", []) or []
            names = [getattr(e, "name", type(e).__name__) for e in engines]
            if names:
                asr_ok = True
                asr_label = "/".join(names)
        self.lbl_asr.setText(f"{_dot(asr_ok)} ASR {asr_label}")

        intent = self._svc.get("intent")
        llm_ok = False
        llm_label = "未就绪"
        if intent is not None:
            llm = getattr(intent, "llm_intent", None)
            if llm is not None and getattr(llm, "is_available", lambda: False)():
                llm_ok = True
                model = getattr(llm, "model", "") or ""
                llm_label = model or "LLM"
        self.lbl_llm.setText(f"{_dot(llm_ok)} LLM {llm_label}")
