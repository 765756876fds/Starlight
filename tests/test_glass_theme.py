"""Glass 全局主题（第一阶段）验收：Token 层 / theme.qss / 主窗口无边框外壳 / 顶栏控制。"""
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from app.ui.theme import tokens
from app.ui.theme.tokens import Tokens
from app.ui.widgets.title_bar import GlassTitleBar


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ---------- Token 层 ----------

class TestTokens:
    def test_radius_and_font(self):
        assert Tokens.RADIUS == 12
        assert "Microsoft YaHei" in Tokens.FONT_FAMILY

    def test_glass_bg_semi_transparent(self):
        # GLASS_BG 必须是 rgba(...) 且 alpha < 255
        assert Tokens.GLASS_BG.startswith("rgba(30, 30, 46,")
        assert Tokens.GLASS_BG_ALPHA < 1.0

    def test_nav_pill_exists(self):
        assert Tokens.NAV_PILL.startswith("rgba(")
        assert Tokens.WINDOW_RADIUS > 0

    def test_colors_reused(self):
        # 基础色必须引用 colors.Colors（单一来源）
        assert Tokens.BG_BASE == tokens.Colors.BG_BASE
        assert Tokens.ACCENT == tokens.Colors.ACCENT


# ---------- theme.qss ----------

class TestThemeQss:
    def test_loadable(self, tmp_path):
        p = Path(__file__).resolve().parents[1] / "app" / "ui" / "theme" / "theme.qss"
        qss = p.read_text(encoding="utf-8")
        assert len(qss) > 500
        assert "QPushButton" in qss and "QScrollBar" in qss

    def test_tab_pill_selected(self, tmp_path):
        p = Path(__file__).resolve().parents[1] / "app" / "ui" / "theme" / "theme.qss"
        qss = p.read_text(encoding="utf-8")
        # 导航选中态必须是胶囊高亮（圆角 + 半透明背景），而非下划线
        assert "QTabBar::tab:selected" in qss
        assert "border-radius: 12px" in qss
        assert "rgba(74, 158, 255, 0.26)" in qss

    def test_global_radius_12(self, tmp_path):
        p = Path(__file__).resolve().parents[1] / "app" / "ui" / "theme" / "theme.qss"
        assert "border-radius: 12px" in p.read_text(encoding="utf-8")

    def test_mainwindow_transparent(self, tmp_path):
        p = Path(__file__).resolve().parents[1] / "app" / "ui" / "theme" / "theme.qss"
        # 主窗口背景透明，由 paintEvent 手绘玻璃底
        assert "QMainWindow { background: transparent; }" in p.read_text(encoding="utf-8")

    def test_titlebar_section(self, tmp_path):
        p = Path(__file__).resolve().parents[1] / "app" / "ui" / "theme" / "theme.qss"
        qss = p.read_text(encoding="utf-8")
        assert "#GlassTitleBar" in qss
        assert "#GlassTitleBar QPushButton#btn_close:hover" in qss

    def test_scrollbar_thin(self, tmp_path):
        p = Path(__file__).resolve().parents[1] / "app" / "ui" / "theme" / "theme.qss"
        # 极简细线半透明滚动条
        assert "width: 6px" in p.read_text(encoding="utf-8")


# ---------- 顶栏 ----------

class TestTitleBar:
    def test_controls_exist(self, qapp):
        tb = GlassTitleBar()
        assert isinstance(tb.btn_min, QPushButton)
        assert isinstance(tb.btn_close, QPushButton)
        assert tb.lbl_title.text() == "LoL Music Agent"

    def test_minimize_and_close_slots(self, qapp, monkeypatch):
        tb = GlassTitleBar()
        called = {}
        monkeypatch.setattr(tb, "_window", lambda: type("W", (), {
            "showMinimized": lambda s: called.setdefault("min", True),
            "close": lambda s: called.setdefault("close", True),
        })())
        tb.btn_min.click()
        assert called.get("min") is True
        tb.btn_close.click()
        assert called.get("close") is True

    def test_drag_moves_window(self, qapp):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QWidget
        host = QWidget()
        host.resize(800, 600)
        tb = GlassTitleBar(parent=host)
        tb.move(10, 10)
        host.show()

        press = QMouseEvent(QEvent.MouseButtonPress, QPointF(50, 20),
                            QPointF(50, 20), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        tb.mousePressEvent(press)
        assert tb._drag_offset is not None  # 按下记录拖动偏移

        release = QMouseEvent(QEvent.MouseButtonRelease, QPointF(50, 20),
                              QPointF(50, 20), Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        tb.mouseReleaseEvent(release)
        assert tb._drag_offset is None  # 松手清除偏移


# ---------- MainWindow Glass 外壳 ----------

class TestMainWindowGlass:
    def test_frameless_and_translucent(self, qapp):
        from app.ui.main_window import MainWindow
        w = MainWindow(music=None)
        flags = w.windowFlags()
        assert flags & Qt.FramelessWindowHint
        assert w.testAttribute(Qt.WA_TranslucentBackground)

    def test_shell_and_titlebar(self, qapp):
        from app.ui.main_window import MainWindow
        w = MainWindow(music=None)
        assert w.centralWidget().objectName() == "GlassShellRoot"
        tb = w.findChild(GlassTitleBar)
        assert tb is not None
        assert w._stack is not None

    def test_glass_theme_applied_to_app(self, qapp):
        from app.ui.main_window import MainWindow
        qapp.setStyleSheet("")  # 清空基线
        MainWindow(music=None)
        sheet = qapp.styleSheet()
        assert "QTabBar::tab:selected" in sheet
        assert "border-radius: 12px" in sheet

    def test_switch_to_tabs_still_works(self, qapp):
        # 回归：无边框外壳不破坏 switch_to_tabs 组装
        from app.ui.main_window import MainWindow
        w = MainWindow(music=None)
        w.switch_to_tabs(None)
        assert w._tabs is not None
        assert w._stack.currentWidget() is w._tabs
