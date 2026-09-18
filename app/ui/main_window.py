"""主窗口。"""
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QMainWindow, QTabWidget, QWidget, QVBoxLayout,
)

from .panels.automation_panel import AutomationPanel
from .panels.cooldown_panel import CooldownPanel
from .panels.music_panel import MusicPanel
from .panels.ongoing_panel import OngoingPanel
from .panels.status_bar import StatusBar


class MainWindow(QMainWindow):
    def __init__(self, automation_service=None, cooldown_manager=None,
                 music=None, ongoing_service=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LoL Music Agent")
        self.resize(560, 720)

        root = QVBoxLayout()
        # 顶栏 overlay 开关
        top = QHBoxLayout()
        self.cb_overlay = QCheckBox("显示游戏内 Overlay")
        self.cb_overlay.setChecked(True)
        top.addWidget(self.cb_overlay)
        top.addStretch()
        root.addLayout(top)

        tabs = QTabWidget()
        tabs.addTab(AutomationPanel(automation_service), "自动化")
        tabs.addTab(OngoingPanel(ongoing_service), "对局信息")
        tabs.addTab(CooldownPanel(cooldown_manager), "计时器")
        tabs.addTab(MusicPanel(music), "音乐")
        root.addWidget(tabs)

        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self._status = StatusBar()
        self.setStatusBar(self._status)

        self._overlay_model = None
        self._overlay_window = None
        self.cb_overlay.toggled.connect(self._on_overlay_toggle)

    def set_overlay_refs(self, model, window) -> None:
        self._overlay_model = model
        self._overlay_window = window
        self.cb_overlay.setChecked(model.is_enabled())

    def _on_overlay_toggle(self, on: bool) -> None:
        if self._overlay_model is not None:
            self._overlay_model.set_enabled(on)
        if not on and self._overlay_window is not None:
            self._overlay_window.hide()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
