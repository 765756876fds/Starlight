from PySide6.QtCore import Qt
from app.ui.theme.colors import Colors

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
)
from app.core.log_bus import get_log_bus


MODULES = [
    ("eventlog", "EventLog"),
    ("music", "Music 模块"),
    ("lol", "LoL 连接"),
    ("tts", "TTS"),
    ("speech", "SpeechQueue"),
    ("asr", "ASR"),
    ("intent", "Intent"),
    ("js", "JS 桥接"),
    ("automation", "自动化"),
]


class LoadingPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels: dict[str, QLabel] = {}
        lay = QVBoxLayout(self)
        title = QLabel("正在启动 LoL Music Agent...")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        lay.addWidget(title)
        for key, name in MODULES:
            row = QHBoxLayout()
            dot = QLabel("○")
            dot.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
            lbl = QLabel(name)
            lbl2 = QLabel("等待...")
            lbl2.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
            row.addWidget(dot)
            row.addWidget(lbl)
            row.addStretch(1)
            row.addWidget(lbl2)
            self._labels[key] = (dot, lbl2)
            lay.addLayout(row)
        lay.addWidget(QLabel("日志:"))
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(200)
        lay.addWidget(self._log, 1)
        get_log_bus().sig_log.connect(self._on_log)

    def _on_log(self, text):
        self._log.appendPlainText(text)

    def set_module_status(self, key: str, status: str):
        if key not in self._labels:
            return
        dot, lbl = self._labels[key]
        if status == "loading":
            dot.setText("◐")
            dot.setStyleSheet("color: #f59e0b;")
            lbl.setText("加载中...")
        elif status == "ok":
            dot.setText("●")
            dot.setStyleSheet("color: #4ade80;")
            lbl.setText("完成")
        elif status == "fail":
            dot.setText("●")
            dot.setStyleSheet("color: #ef4444;")
            lbl.setText("失败")
