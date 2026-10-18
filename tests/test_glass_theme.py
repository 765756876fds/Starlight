"""Glass 全局主题验收：Token 层 / build_glass_qss() 生成的 QSS / 主窗口无边框外壳 /
顶栏控制 / MusicPanel 卡片网格（Delegate）与键盘快捷键。"""
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QApplication, QListView, QListWidget, QListWidgetItem, QPushButton,
    QStyleOptionViewItem,
)

from app.ui.theme import tokens
from app.ui.theme.applier import build_glass_qss
from app.ui.theme.tokens import Tokens
from app.ui.widgets.title_bar import GlassTitleBar
from app.ui.widgets.music_card_delegate import (
    MusicCardDelegate, MusicCardRoles, cover_key_of,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _rel_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    comps = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in comps]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


# ---------- Token 层 ----------

class TestTokens:
    def test_radius_and_font(self):
        assert Tokens.RADIUS == 10
        assert "Microsoft YaHei" in Tokens.FONT_FAMILY

    def test_type_scale(self):
        # 排版阶梯：12 辅助 / 14 正文 / 16 小标题 / 24 大标题
        assert (Tokens.FONT_SIZE_SUB, Tokens.FONT_SIZE_BODY,
                Tokens.FONT_SIZE_TITLE, Tokens.FONT_SIZE_HERO) == (12, 14, 16, 24)

    def test_spacing_grid(self):
        # 8px 间距网格
        assert Tokens.SPACING_SM == 8
        assert Tokens.SPACING_MD == 2 * Tokens.SPACING_SM

    def test_card_tokens(self):
        assert Tokens.CARD_WIDTH == 160 and Tokens.CARD_HEIGHT == 200
        assert Tokens.COVER_SIZE == Tokens.CARD_WIDTH - 2 * Tokens.CARD_PADDING

    def test_glass_bg_semi_transparent(self):
        assert Tokens.GLASS_BG.startswith("rgba(30, 30, 46,")
        assert Tokens.GLASS_BG_ALPHA < 1.0

    def test_nav_pill_exists(self):
        assert Tokens.NAV_PILL.startswith("rgba(")
        assert Tokens.WINDOW_RADIUS > 0

    def test_colors_reused(self):
        # 基础色必须引用 colors.Colors（单一来源）
        assert Tokens.BG_BASE == tokens.Colors.BG_BASE
        assert Tokens.ACCENT == tokens.Colors.ACCENT
        assert Tokens.BG_BASE_RGB == "30, 30, 46"   # RGB 派生与色板一致

    def test_secondary_contrast_aa(self):
        # 辅助文字在卡片底（BG_ELEVATED）上 ≥ WCAG AA 4.5:1
        from app.ui.theme import colors
        ratio = (_rel_luminance(colors.Colors.TEXT_SECONDARY) + 0.05) / \
                (_rel_luminance(colors.Colors.BG_ELEVATED) + 0.05)
        assert ratio >= 4.5

    def test_glass_card_alpha(self):
        # 玻璃呼吸感：常态更通透、hover 略实
        assert Tokens.CARD_ALPHA < 0.9
        assert Tokens.CARD_HOVER_ALPHA > Tokens.CARD_ALPHA
        assert 0 < Tokens.CARD_ALPHA < Tokens.CARD_HOVER_ALPHA < 1.0

    def test_card_gradient_tokens(self):
        # 渐变受光：顶部色亮于底部色（同族内）
        assert Tokens.CARD_TOP_RGB == Tokens.BG_HOVER_RGB
        assert Tokens.CARD_BOTTOM_RGB == Tokens.BG_ELEVATED_RGB
        assert Tokens.GLASS_TOP_LINE.startswith("rgba(255, 255, 255,")
        assert Tokens.GLASS_BOTTOM_LINE.startswith("rgba(0, 0, 0,")

    def test_placeholder_contrast(self):
        # placeholder 复用辅助文字色（WCAG 覆盖，impeccable）
        assert Tokens.PLACEHOLDER == tokens.Colors.TEXT_SECONDARY


# ---------- build_glass_qss()（theme.qss 文件已移除，样式由 Tokens 生成） ----------

class TestGlassQss:
    def test_loadable(self):
        qss = build_glass_qss()
        assert len(qss) > 500
        assert "QPushButton" in qss and "QScrollBar" in qss

    def test_tab_pill_selected(self):
        qss = build_glass_qss()
        assert "QTabBar::tab:selected" in qss
        assert "border-radius: 10px" in qss
        assert "rgba(74, 158, 255" in qss

    def test_global_radius_10(self):
        qss = build_glass_qss()
        assert "border-radius: 10px" in qss

    def test_no_non_scale_font_sizes(self):
        # QSS 中不得出现非排版阶梯的字面量字号
        qss = build_glass_qss()
        assert "font-size: 11px" not in qss
        assert "font-size: 13px" not in qss
        assert "font-size: 18px" not in qss

    def test_six_state_buttons(self):
        qss = build_glass_qss()
        for st in ("QPushButton:hover", "QPushButton:pressed",
                   "QPushButton:focus", "QPushButton:disabled",
                   'QPushButton[class="primary"]:hover',
                   'QPushButton[class="primary"]:focus'):
            assert st in qss

    def test_playback_main_button(self):
        qss = build_glass_qss()
        assert "QPushButton#btn_toggle" in qss
        assert f"min-width: {Tokens.BTN_MAIN}px" in qss
        assert f"border-radius: {Tokens.BTN_MAIN // 2}px" in qss

    def test_focus_highlight(self):
        qss = build_glass_qss()
        assert "QLineEdit:focus" in qss and Tokens.ACCENT in qss
        assert "QPushButton:focus" in qss

    def test_placeholder_uses_secondary(self):
        # placeholder 颜色来自 Token（WCAG 对比，非暗灰字面量）
        qss = build_glass_qss()
        assert "QLineEdit::placeholder" in qss
        assert f"QLineEdit::placeholder {{ color: {Tokens.PLACEHOLDER}; }}" in qss
        assert Tokens.TEXT_DISABLED not in qss.split("QLineEdit::placeholder")[1][:80]

    def test_mainwindow_transparent(self):
        qss = build_glass_qss()
        assert "QMainWindow { background: transparent; }" in qss

    def test_titlebar_section(self):
        qss = build_glass_qss()
        assert "#GlassTitleBar" in qss
        assert "#GlassTitleBar QPushButton#btn_close:hover" in qss

    def test_scrollbar_thin(self):
        qss = build_glass_qss()
        assert "width: 6px" in qss


# ---------- 卡片 Delegate ----------

class TestMusicCardDelegate:
    def test_size_hint(self, qapp):
        view = QListWidget()
        view.addItem("x")
        idx = view.model().index(0, 0)
        d = MusicCardDelegate(view)
        sh = d.sizeHint(QStyleOptionViewItem(), idx)
        assert sh.width() == Tokens.CARD_WIDTH
        assert sh.height() == Tokens.CARD_HEIGHT

    def test_hover_animation_150ms_easeout(self, qapp):
        d = MusicCardDelegate()
        assert d._anim.duration() == 150
        from PySide6.QtCore import QEasingCurve
        # diseno-qt：内置 OutCubic 过弱 → BezierSpline (0.23,1,0.32,1)
        assert d._anim.easingCurve().type() == QEasingCurve.Type.BezierSpline

    def test_paint_renders(self, qapp):
        view = QListWidget()
        view.setViewMode(QListView.IconMode)
        d = MusicCardDelegate(view)
        view.setItemDelegate(d)
        view.setSpacing(Tokens.SPACING_MD)
        for i, title in enumerate(("短标题", "这是一首很长的歌名用于测试省略号截断显示效果")):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, {
                "title": title, "artist": "测试歌手", "_sub": "测试歌手 · 来源",
                "_play_key": f"local:{i + 1}", "cover_url": "",
            })
            view.addItem(item)
        view.resize(420, 320)
        view.show()
        QApplication.processEvents()
        pm = view.grab()
        assert not pm.isNull()

    def test_paint_playing_and_status(self, qapp):
        view = QListWidget()
        view.setViewMode(QListView.IconMode)
        d = MusicCardDelegate(view)
        view.setItemDelegate(d)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, {
            "title": "歌", "artist": "人", "_sub": "人", "_play_key": "local:1",
        })
        view.addItem(item)
        d.playing_key = "local:1"
        item.setData(MusicCardRoles.StatusRole, "下载中 42%")
        view.resize(240, 240)
        view.show()
        QApplication.processEvents()
        pm = view.grab()
        assert not pm.isNull()

    def test_cover_key_of(self):
        assert cover_key_of({"cover_url": "http://x/a.jpg"}) == "net:http://x/a.jpg"
        assert cover_key_of({"path": r"C:\m\a.mp3"}) == "local:C:\\m\\a.mp3"
        assert cover_key_of({"title": "x"}) is None
        assert cover_key_of(None) is None


# ---------- 动效规范（diseno-qt §3/§6 + ui-ux-pro-max motion） ----------

class TestMotion:
    def test_ease_out_is_bezier(self):
        from PySide6.QtCore import QEasingCurve
        from app.ui.theme import motion
        # 内置 OutCubic 过弱 → Emil 曲线 (0.23,1,0.32,1) BezierSpline
        assert motion.EASE_OUT.type() == QEasingCurve.Type.BezierSpline
        assert motion.EASE_IN_OUT.type() == QEasingCurve.Type.BezierSpline
        assert motion.DUR_HOVER == 150
        assert 1200 <= motion.SHIMMER_DURATION <= 1600  # motion.csv #15

    def test_reduced_motion_override(self, qapp):
        from app.ui.theme import motion
        motion.set_reduced_motion(True)
        assert motion.reduced_motion_enabled() is True
        motion.set_reduced_motion(False)
        assert motion.reduced_motion_enabled() is False
        motion.set_reduced_motion(None)  # 恢复跟随系统，勿影响其他测试

    def test_shimmer_clock_acquire_release(self, qapp):
        from app.ui.theme import motion
        clock = motion.ShimmerClock.instance()
        assert clock.active() is False
        motion.set_reduced_motion(True)
        clock.acquire()
        assert clock.active() is False          # reduced-motion：静态骨架
        clock.release()
        motion.set_reduced_motion(False)
        clock.acquire()
        assert clock.active() is True           # 有 pending 封面 → 微光运行
        clock.release()
        assert clock.active() is False          # 全部就绪 → 时钟停
        motion.set_reduced_motion(None)
        assert clock.pos() == 0.0

    def test_cover_loader_pending_drives_shimmer(self, qapp, monkeypatch):
        from PySide6.QtWidgets import QListWidget
        from app.ui.theme import motion
        from app.ui.widgets import music_card_delegate as mcd
        view = QListWidget()
        d = mcd.MusicCardDelegate(view)
        clock = motion.ShimmerClock.instance()
        motion.set_reduced_motion(False)
        # 本地封面路径不存在 → request 入队后失败 → pending 归零 → 时钟停止
        d._loader.request("local:C:/__nope__/a.mp3")
        assert clock.active() is True
        QApplication.processEvents()
        QApplication.processEvents()
        deadline = 0
        while clock.active() and deadline < 200:
            QApplication.processEvents()
            deadline += 1
        assert clock.active() is False
        motion.set_reduced_motion(None)


# ---------- MusicPanel：卡片网格 + 快捷键 ----------

class TestMusicPanelCards:
    def test_card_views_icon_mode(self, qapp):
        from app.ui.panels.music_panel import MusicPanel
        p = MusicPanel(music=None)
        for w in (p.list_local, p.list_results, p.list_history,
                  p.list_playlist_songs, p.list_rank_songs):
            assert w.viewMode() == QListView.IconMode, w
            assert w.spacing() == Tokens.SPACING_MD
        assert p._dl_local is not None

    def test_card_data_attached(self, qapp):
        from app.ui.panels.music_panel import MusicPanel
        p = MusicPanel(music=None)
        d = p._card_data({"title": "a"}, "b", "local:1")
        assert d["_sub"] == "b" and d["_play_key"] == "local:1"
        assert d["title"] == "a"
        assert p._play_key({"id": 3}) == "local:3"
        assert p._play_key({"online": True, "track_id": "t1"}) == "net:t1"
        assert p._play_key(None) is None

    def test_ctrl_f_shortcut_focuses_search(self, qapp):
        from app.ui.panels.music_panel import MusicPanel
        p = MusicPanel(music=None)
        assert p._sc_find is not None and p._sc_k is not None
        p.show()
        QApplication.processEvents()
        p.tabs.setCurrentIndex(0)
        p._focus_search()
        assert p.edit_local.hasFocus()
        p.tabs.setCurrentIndex(1)
        p._focus_search()
        assert p.edit_kw.hasFocus()


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
        assert "border-radius: 10px" in sheet

    def test_switch_to_tabs_still_works(self, qapp):
        # 回归：无边框外壳不破坏 switch_to_tabs 组装
        from app.ui.main_window import MainWindow
        w = MainWindow(music=None)
        w.switch_to_tabs(None)
        assert w._tabs is not None
        assert w._stack.currentWidget() is w._tabs
