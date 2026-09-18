"""主窗口。"""
from PySide6.QtWidgets import QMainWindow, QTabWidget, QWidget, QVBoxLayout

from .panels.automation_panel import AutomationPanel
from .panels.cooldown_panel import CooldownPanel
from .panels.music_panel import MusicPanel
from .panels.status_bar import StatusBar


class MainWindow(QMainWindow):
    def __init__(self, automation_service=None, cooldown_manager=None,
                 music=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LoL Music Agent")
        self.resize(480, 640)

        tabs = QTabWidget()
        tabs.addTab(AutomationPanel(automation_service), "自动化")
        tabs.addTab(CooldownPanel(cooldown_manager), "计时器")
        tabs.addTab(MusicPanel(music), "音乐")
        self.setCentralWidget(tabs)

        self._status = StatusBar()
        self.setStatusBar(self._status)

    def closeEvent(self, event):
        # 关闭 → 隐藏到托盘，不退出
        event.ignore()
        self.hide()
