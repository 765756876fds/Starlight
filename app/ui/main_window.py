"""主窗口。"""
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QMainWindow, QStackedWidget, QTabWidget,
    QWidget, QVBoxLayout,
)

from .panels.automation_panel import AutomationPanel
from .panels.cooldown_panel import CooldownPanel
from .panels.music_panel import MusicPanel
from .panels.ongoing_panel import OngoingPanel
from .panels.status_bar import StatusBar
from .panels.loading_panel import LoadingPanel


class MainWindow(QMainWindow):
    def __init__(self, automation_service=None, cooldown_manager=None,
                 music=None, ongoing_service=None, services=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LoL Music Agent")
        self.resize(500, 680)
        self._services = services or {}

        # 自动恢复上次窗口大小和位置
        from PySide6.QtCore import QSettings
        self._settings = QSettings("LoLMusicAgent", "MainWindow")
        geom = self._settings.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        size = self._settings.value("size")
        if size:
            self.resize(size)

        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        # Page 0: loading
        self._loading = LoadingPanel()
        self._stack.addWidget(self._loading)

        # Page 1: tabs (built lazily via switch_to_tabs)
        self._tabs = None
        self._automation_service = automation_service
        self._cooldown_manager = cooldown_manager
        self._music = music
        self._ongoing_service = ongoing_service

        try:
            from app.core.log_bus import get_log_bus
            get_log_bus().sig_log.connect(self._loading._on_log)
        except Exception:
            pass

    def switch_to_tabs(self, tabs=None):
        print("[UI] switch_to_tabs")
        if tabs is None:
            # Build tabs ourselves
            central = QWidget()
            root = QVBoxLayout(central)
            top = QHBoxLayout()
            self.cb_overlay = QCheckBox("显示游戏内 Overlay")
            self.cb_overlay.setChecked(True)
            top.addWidget(self.cb_overlay)
            top.addStretch()
            root.addLayout(top)
            tabs_w = QTabWidget()
            tabs_w.addTab(AutomationPanel(self._automation_service), "自动化")
            tabs_w.addTab(OngoingPanel(self._ongoing_service, account_history_service=self._build_account_history()), "对局信息")
            # tabs_w.addTab(CooldownPanel(self._cooldown_manager), "计时器")  # 隐藏计时器tab
            tabs_w.addTab(MusicPanel(self._music), "音乐")
            root.addWidget(tabs_w)
            self._tabs = central
        else:
            self._tabs = tabs
        self._stack.addWidget(self._tabs)
        self._stack.setCurrentIndex(self._stack.count() - 1)
        self._status = StatusBar(self._services)
        self.setStatusBar(self._status)
        self._overlay_model = None
        self._overlay_window = None
        try:
            self.cb_overlay.toggled.connect(self._on_overlay_toggle)
        except Exception:
            pass
        print("[UI] switched")

    def _build_account_history(self):
        try:
            lcu_get = None
            lcu = self._services.get("lcu") if isinstance(self._services, dict) else None
            if lcu is not None and hasattr(lcu, "get"):
                lcu_get = lcu.get
            elif self._ongoing_service is not None:
                lcu = getattr(self._ongoing_service, "_lcu", None)
                if lcu is not None and hasattr(lcu, "get"):
                    lcu_get = lcu.get
            if lcu_get is None:
                return None
            from app.lol.ongoing.account_history import AccountMatchHistoryService
            return AccountMatchHistoryService(lcu_get)
        except Exception:
            return None

    def set_overlay_refs(self, model, window) -> None:
        self._overlay_model = model
        self._overlay_window = window
        if hasattr(self, "cb_overlay"):
            self.cb_overlay.setChecked(model.is_enabled())

    def _on_overlay_toggle(self, on: bool) -> None:
        if self._overlay_model is not None:
            self._overlay_model.set_enabled(on)
        if not on and self._overlay_window is not None:
            self._overlay_window.hide()

    def closeEvent(self, event):
        event.ignore()
        self.hide()





