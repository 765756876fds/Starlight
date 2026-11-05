from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.log_bus import get_log_bus
from app.ui.theme.colors import Colors
from app.ui.theme.tokens import Tokens

MODULES = [
    ("eventlog", "运行记录"),
    ("music", "音乐播放器"),
    ("lol", "游戏服务"),
    ("tts", "语音播报"),
    ("speech", "播报队列"),
    ("asr", "语音识别"),
    ("intent", "语音指令"),
    ("js", "在线音乐音源"),
    ("automation", "自动化"),
]


class LoadingPanel(QWidget):
    # 跨线程状态更新：backend 线程 emit，AutoConnection 自动 queued 到 UI 线程
    status_changed = Signal(str, str)  # (key, status)
    # 启动兜底：任何单点状态（backend 异常/切换失败）卡死 15s 后强制进入主界面
    boot_timeout = Signal()
    # 启动失败：backend 线程 emit，UI 线程显示错误摘要（不切页）
    boot_failed = Signal(str)
    # 启动成功：backend 线程 emit，UI 线程切到功能页
    boot_succeeded = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels: dict[str, QLabel] = {}
        lay = QVBoxLayout(self)
        title = QLabel("正在启动星芒…")
        title.setStyleSheet(f"font-size: {Tokens.FONT_SIZE_TITLE}px; font-weight: bold;")
        lay.addWidget(title)
        for key, name in MODULES:
            row = QHBoxLayout()
            dot = QLabel("○")
            dot.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
            lbl = QLabel(name)
            lbl2 = QLabel("等待...")
            lbl2.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
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
        self.status_changed.connect(self.set_module_status)
        # 兜底：15s 未完成启动（后台异常/切换失败/信号断开）→ 强制进入主界面
        QTimer.singleShot(15_000, self._on_boot_timeout)

    def _on_boot_timeout(self):
        self.boot_timeout.emit()

    def _on_log(self, text):
        self._log.appendPlainText(text)

    def show_failure(self, summary: str):
        """启动失败：在日志区顶部插入红色错误摘要，不切页。"""
        self._log.appendPlainText("")
        self._log.appendPlainText(f"[启动失败] {summary}")

    def set_module_status(self, key: str, status: str):
        if key not in self._labels:
            return
        dot, lbl = self._labels[key]
        if status == "loading":
            dot.setText("◐")
            dot.setStyleSheet(f"color: {Colors.WARNING};")
            lbl.setText("加载中...")
        elif status == "ok":
            dot.setText("●")
            dot.setStyleSheet(f"color: {Colors.SUCCESS};")
            lbl.setText("完成")
        elif status == "fail":
            dot.setText("●")
            dot.setStyleSheet(f"color: {Colors.DANGER};")
            lbl.setText("失败")
        elif status == "disabled":
            dot.setText("○")
            dot.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
            lbl.setText("未启用")
