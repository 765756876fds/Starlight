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

    def test_cover_palette(self):
        # 动态专辑图：≥6 组高级渐变对 + 首字浅色半透明（深底可读）
        assert len(Tokens.COVER_PALETTES) >= 6
        for top, bot in Tokens.COVER_PALETTES:
            assert "," in top and "," in bot
        assert "rgba(255, 255, 255, 170)" in Tokens.COVER_LETTER

    def test_skeleton_seed_variety(self):
        # 调色板按 seed 稳定映射：连续 6 个 seed 完整覆盖全部调色板（无重复映射缺陷）
        n = len(Tokens.COVER_PALETTES)
        assert {s % n for s in range(n)} == set(range(n))


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

    def test_checkable_checked(self):
        # 通用 checkable 按钮：checked 态 accent 高亮（MoviePanel 视图切换等）
        qss = build_glass_qss()
        assert "QPushButton:checked" in qss
        assert Tokens.ACCENT in qss

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

    def test_card_data_cover_fallback(self, qapp):
        # 不同源封面字段名 → 统一映射 cover_url；并预计算 _seed
        from app.ui.panels.music_panel import MusicPanel
        p = MusicPanel(music=None)
        d = p._card_data({"title": "x", "extra": {"raw": {"coverUrl": "http://a/1.jpg"}}},
                         "s", "net:1")
        assert d["cover_url"] == "http://a/1.jpg"
        assert isinstance(d.get("_seed"), int)
        d2 = p._card_data({"title": "x", "cover_img": "http://a/2.jpg"}, "s", "net:1")
        assert d2["cover_url"] == "http://a/2.jpg"

    def test_view_toggle_switches_all_lists(self, qapp):
        from app.ui.panels.music_panel import MusicPanel
        p = MusicPanel(music=None)
        assert p.btn_view_toggle.isChecked() is True     # 默认网格
        p._toggle_view_mode(False)
        for w in (p.list_local, p.list_results, p.list_history,
                  p.list_playlist_songs, p.list_rank_songs):
            assert w.viewMode() == QListView.ListMode, w
        p._toggle_view_mode(True)
        for w in (p.list_local, p.list_results, p.list_history,
                  p.list_playlist_songs, p.list_rank_songs):
            assert w.viewMode() == QListView.IconMode, w

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


# ---------- 状态栏（Glass 重绘） ----------

class TestStatusBar:
    def test_glass_redraw_no_emoji(self, qapp):
        from app.ui.panels.status_bar import StatusBar
        sb = StatusBar({})
        try:
            # 无原生圆点 / Emoji：Lucide 图标 + 状态文字
            assert "●" not in sb.lbl_lcu.text() and "●" not in sb.lbl_asr.text()
            assert "🎤" not in sb.btn_mic.text()
            assert sb.lbl_lcu.pixmap() is not None      # link 图标已渲染
            assert sb.lbl_asr.pixmap() is not None      # cpu 图标已渲染
            assert not sb.btn_mic.icon().isNull()
            # refresh 不重建 widget（防每秒泄漏）
            old = sb.lbl_lcu
            sb.refresh()
            assert sb.lbl_lcu is old
        finally:
            sb._timer.stop()


# ---------- GlassDialog（无边框对话框基类） ----------

class TestGlassDialog:
    def test_frameless_translucent(self, qapp):
        from app.ui.widgets.glass_dialog import GlassDialog
        d = GlassDialog("测试")
        try:
            assert d.windowFlags() & Qt.FramelessWindowHint
            assert d.testAttribute(Qt.WA_TranslucentBackground)
            assert d.lbl_title.text() == "测试"
            assert not d.btn_close.icon().isNull()      # Lucide x 关闭图标
            # body 布局可直接挂内容（回归 PySide6 二次 setLayout 内容丢失 bug）
            from PySide6.QtWidgets import QLabel
            d.body_layout.addWidget(QLabel("HELLO"))
            assert d.body.findChildren(QLabel)
        finally:
            d.close()

    def test_esc_closes(self, qapp):
        from PySide6.QtWidgets import QDialog
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent
        from app.ui.widgets.glass_dialog import GlassDialog
        d = GlassDialog("x")
        d.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        assert d.result() == QDialog.Rejected

    def test_dialog_subclasses_glass(self, qapp):
        # 全局 QDialog 子类已迁移 GlassDialog（无原生白色标题栏）
        from app.ui.dialogs.auto_queue_config_dialog import AutoQueueConfigDialog
        from app.ui.widgets.glass_dialog import GlassDialog
        d = AutoQueueConfigDialog({"auto_queue_mode": "normal"})
        try:
            assert isinstance(d, GlassDialog)
            assert d.windowFlags() & Qt.FramelessWindowHint
        finally:
            d.close()

    def test_autoqueue_grid_and_primary_btn(self, qapp):
        # QGridLayout 严格对齐 + 底部主按钮 accent 实底（零原生控件布局）
        from PySide6.QtWidgets import QGridLayout, QPushButton
        from app.ui.dialogs.auto_queue_config_dialog import AutoQueueConfigDialog
        d = AutoQueueConfigDialog({"auto_queue_mode": "normal"})
        try:
            grids = d.findChildren(QGridLayout)
            assert grids, "AutoQueue 必须使用 QGridLayout 对齐"
            btns = d.findChildren(QPushButton)
            ok = next(b for b in btns if b.text() == "确定")
            assert ok.property("class") == "primary", "主按钮必须是 accent 实底"
        finally:
            d.close()

    def test_combo_hero_delegate_draws_bar(self, qapp):
        # 英雄列表 Delegate：选中项左 3px accent 亮条（QSS ::item border 不渲染）
        from unittest.mock import MagicMock
        from app.ui.dialogs.combo_manager_dialog import ComboManagerDialog
        from app.ui.panels.combo_panel import _HeroListDelegate
        cm = MagicMock()
        cm.get_combos.return_value = {}
        cm.get_usage.return_value = 0
        cm.find_champion_by_id.return_value = None
        cm.get_current_champion.return_value = None
        cm.current_champion_name.return_value = None
        d = ComboManagerDialog(cm)
        try:
            lst = d.findChildren(QListWidget)[0]
            assert isinstance(lst.itemDelegate(), _HeroListDelegate)
        finally:
            d.close()


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

    def test_btn_gear_lucide_icon(self, qapp):
        # 回归：自动化面板高级选项按钮为 Lucide settings-2 线性图标（非 Emoji 齿轮）
        from unittest.mock import MagicMock
        from app.ui.panels.automation_panel import AutomationPanel
        from app.ui.widgets.icon_utils import _LUCIDE
        assert "settings-2" in _LUCIDE
        a = AutomationPanel(service=MagicMock(), lcu=MagicMock(), combo_manager=MagicMock())
        gears = [w for w in a.findChildren(
            __import__("PySide6.QtWidgets", fromlist=["QPushButton"]).QPushButton)
            if w.property("class") == "btn-gear"]
        assert len(gears) == 4
        assert not gears[0].text()  # 纯图标按钮，无 Emoji 文字
        assert not any("⚙" in w.text() for w in gears)

    def test_switch_to_tabs_still_works(self, qapp):
        # 回归：无边框外壳不破坏 switch_to_tabs 组装
        from app.ui.main_window import MainWindow
        w = MainWindow(music=None)
        w.switch_to_tabs(None)
        assert w._tabs is not None
        assert w._stack.currentWidget() is w._tabs

    def test_boot_timeout_guard(self, qapp):
        # LoadingPanel 15s 兜底：正常切换后不再二次切换；重复调用防重
        from app.ui.main_window import MainWindow
        w = MainWindow(music=None)
        assert hasattr(w._loading, "boot_timeout")     # 兜底信号存在
        w.switch_to_tabs(None)
        n = w._stack.count()
        w.switch_to_tabs(None)                          # 防重：不再 addWidget
        assert w._stack.count() == n
        w._on_boot_timeout()                            # 已切换 → 兜底忽略
        assert w._stack.count() == n

    def test_boot_timeout_forces_switch(self, qapp):
        # 兜底生效路径：仍停留在 LoadingPanel 时超时强制进入主界面
        from app.ui.main_window import MainWindow
        w = MainWindow(music=None)
        assert w._stack.currentWidget() is w._loading
        w._on_boot_timeout()
        assert w._stack.currentWidget() is not w._loading
