"""状态栏：实时读服务状态。"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QPushButton, QStatusBar


def _dot(ok: bool) -> str:
    color = "#4ade80" if ok else "#ef4444"
    return f'<span style="color:{color};font-size:14px;">●</span>'


class StatusBar(QStatusBar):
    def __init__(self, services=None, parent=None):
        super().__init__(parent)
        self._svc = services or {}
        self.lbl_lcu = QLabel()
        self.lbl_asr = QLabel()
        self.btn_mic = QPushButton("🎤 关")
        self.btn_mic.setCheckable(True)
        self.btn_mic.setStyleSheet("padding: 2px 8px;")
        self.btn_mic.clicked.connect(self._toggle_mic)
        for lbl in (self.lbl_lcu, self.lbl_asr):
            lbl.setTextFormat(Qt.RichText)
            lbl.setStyleSheet("padding: 0 8px;")
            self.addWidget(lbl)
        self.addWidget(self.btn_mic)
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
            # 真实健康：active engine 是否可连通（8080 health）
            if hasattr(asr, "is_available") and asr.is_available():
                asr_ok = True
                engines = getattr(asr, "engines", []) or []
                names = [getattr(e, "name", type(e).__name__) for e in engines]
                asr_label = "/".join(names) if names else "READY"
        self.lbl_asr.setText(f"{_dot(asr_ok)} ASR {asr_label}")

    def _toggle_mic(self):
        try:
            voice = self._svc.get("voice")
            if voice:
                on = voice.toggle_mic()
                self.btn_mic.setText("🎤 开" if on else "🎤 关")
        except Exception as e:
            print(f"麦克风开关错误: {e}")
