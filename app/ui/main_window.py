"""主窗口（Glass 无边框 + 半透明圆角 + 自定义顶栏）。"""
import os
import sys
from pathlib import Path

try:
    import ctypes
    from ctypes import windll, wintypes
    _WIN32 = sys.platform.startswith("win")
except Exception:
    _WIN32 = False

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QMainWindow, QStackedWidget, QTabWidget,
    QWidget, QVBoxLayout,
)

from .panels.automation_panel import AutomationPanel
from .panels.cooldown_panel import CooldownPanel
from .panels.music_panel import MusicPanel
from .panels.ongoing_panel import OngoingPanel
from .panels.status_bar import StatusBar
from .panels.loading_panel import LoadingPanel
from .theme.tokens import Tokens
from .widgets.title_bar import GlassTitleBar

# ---- Windows 无边框窗口边缘缩放（WM_NCHITTEST） ----
_WM_NCHITTEST = 0x0084
_HT_CLIENT = 1
_HT_LEFT = 10
_HT_RIGHT = 11
_HT_TOP = 12
_HT_TOPLEFT = 13
_HT_TOPRIGHT = 14
_HT_BOTTOM = 15
_HT_BOTTOMLEFT = 16
_HT_BOTTOMRIGHT = 17
_RESIZE_BORDER_PX = 5  # 窗口边缘 5px 内视为缩放手柄，恢复原生拖拽缩放


def _nchit_edge(lx: int, ly: int, w: int, h: int, border: int = _RESIZE_BORDER_PX) -> int:
    """把鼠标相对窗口左上角的物理像素坐标映射为 WM_NCHITTEST 命中码（纯函数）。

    lx/ly 以窗口物理像素为原点；w/h 为窗口物理像素尺寸；border 为缩放手柄宽度。
    """
    left = lx <= border
    right = lx >= w - 1 - border
    top = ly <= border
    bottom = ly >= h - 1 - border
    if top and left:
        return _HT_TOPLEFT
    if top and right:
        return _HT_TOPRIGHT
    if bottom and left:
        return _HT_BOTTOMLEFT
    if bottom and right:
        return _HT_BOTTOMRIGHT
    if left:
        return _HT_LEFT
    if right:
        return _HT_RIGHT
    if top:
        return _HT_TOP
    if bottom:
        return _HT_BOTTOM
    return _HT_CLIENT


class MainWindow(QMainWindow):
    def __init__(self, automation_service=None, cooldown_manager=None,
                 music=None, ongoing_service=None, services=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("星芒")
        # 应用自定义图标（源码态 = 项目根 assets/icon/app.ico；
        # 打包态 spec 收集到 _internal/assets/icon/app.ico，__file__ 相对自动命中）
        _icon_path = str(Path(__file__).resolve().parents[2] / "assets" / "icon" / "app.ico")
        if os.path.isfile(_icon_path):
            self.setWindowIcon(QIcon(_icon_path))
        self.resize(1100, 720)
        self._services = services or {}

        # ---- Glass 主框架：无边框 + 半透明圆角（外壳，不动任何面板逻辑） ----
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 全局套用 Glass 样式（覆盖 main.py 的 dark.qss；测试环境 qapp 已存在则同样生效）
        app = QApplication.instance()
        if app is not None:
            try:
                from .theme.applier import apply_glass_theme
                apply_glass_theme(app)
            except Exception as e:
                print(f"[UI] Glass 主题应用失败（降级，不影响运行）: {e}")
        self._shell = QWidget()
        self._shell.setObjectName("GlassShellRoot")
        self._shell_layout = QVBoxLayout(self._shell)
        self._shell_layout.setContentsMargins(0, 0, 0, 0)
        self._shell_layout.setSpacing(0)
        self._title_bar = GlassTitleBar("LoL Music Agent")
        self._shell_layout.addWidget(self._title_bar)

        # 自动恢复上次窗口大小和位置
        self._settings = QSettings("LoLMusicAgent", "MainWindow")
        geom = self._settings.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        size = self._settings.value("size")
        if size:
            self.resize(size)

        self._stack = QStackedWidget()
        self._shell_layout.addWidget(self._stack, 1)
        self.setCentralWidget(self._shell)

        # Page 0: loading
        self._loading = LoadingPanel()
        self._boot_done = False
        self._loading.boot_timeout.connect(self._on_boot_timeout)
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

    def _on_boot_timeout(self):
        """启动兜底：LoadingPanel 15s 未完成 → 强制进入主界面（仅仍停留在 loading 时）。"""
        if self._stack.currentWidget() is self._loading:
            print("[UI] boot timeout -> force switch_to_tabs")
            self.switch_to_tabs()

    def switch_to_tabs(self, tabs=None):
        print("[UI] switch_to_tabs")
        if self._boot_done:
            print("[UI] switch_to_tabs skipped (already booted)")
            return
        self._boot_done = True
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

    # ---- Glass 外壳：绘制深色半透明圆角底（最大化时铺满直角） ----
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        if self.isMaximized():
            path = QPainterPath()
            path.addRect(rect)
        else:
            r = Tokens.WINDOW_RADIUS
            path = QPainterPath()
            path.addRoundedRect(rect, r, r)
        painter.setPen(QColor(74, 158, 255, 60))  # 高光细边
        bg = QColor(30, 30, 46, int(255 * Tokens.GLASS_BG_ALPHA))
        painter.fillPath(path, bg)
        painter.drawPath(path)
        super().paintEvent(event)

    def closeEvent(self, event):
        # 记忆窗口几何与尺寸（隐藏到托盘/关闭前保存，重启后恢复）
        try:
            self._settings.setValue("geometry", self.saveGeometry())
            self._settings.setValue("size", self.size())
        except Exception as e:
            print(f"[UI] 保存窗口尺寸失败（忽略）: {e}")
        event.ignore()
        self.hide()

    def nativeEvent(self, eventType, message):
        """Windows 无边框窗口边缘缩放：拦截 WM_NCHITTEST，边缘 5px 内返回缩放手柄。

        最大化状态不参与缩放（Windows 原生也不允许拖拽调整最大化窗口）；
        任何解析异常降级走默认处理，绝不阻断 Qt 事件流。
        """
        if _WIN32:
            try:
                # PySide6 各版本 eventType 可能是 QByteArray/bytes/str，兼容处理
                et = eventType
                if hasattr(et, "data"):       # QByteArray
                    et = et.data()
                if isinstance(et, bytes):
                    et = et.decode("latin-1")
                if et == "windows_generic_MSG":
                    msg = wintypes.MSG.from_address(int(message))
                    if msg.message == _WM_NCHITTEST:
                        if self.isMaximized():
                            return True, _HT_CLIENT
                        # lParam 低/高 16 位为鼠标屏幕坐标（物理像素，有符号）
                        sx = ctypes.c_short(msg.lParam & 0xFFFF).value
                        sy = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                        # ctypes.wintypes.MSG 的句柄字段名跨版本不同（hWnd / hwnd）
                        hwnd = getattr(msg, "hwnd", None)
                        if hwnd is None:
                            hwnd = getattr(msg, "hWnd", 0)
                        rect = wintypes.RECT()
                        windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        hit = _nchit_edge(
                            sx - rect.left, sy - rect.top,
                            rect.right - rect.left, rect.bottom - rect.top,
                        )
                        return True, hit
            except Exception:
                pass
        try:
            return super().nativeEvent(eventType, message)
        except Exception:
            # 解析/转发异常时降级为未处理，绝不阻断事件流
            return False, 0





