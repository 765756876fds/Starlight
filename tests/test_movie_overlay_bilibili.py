"""电影内嵌 S7：overlay 控制条对标 B站 三点验收测试（offscreen）。

验收：
- 点1 置顶：overlay/侧栏无永久置顶（WindowStaysOnTopHint 已去），owner =
  播放窗口（Qt parent）——被盖跟随播放窗口，但仍浮在 mpv 画面之上；
  Tool | Frameless 保留。
- 点2 透明：WA_TranslucentBackground 已设；QSS 无实心填充/无黑边框；渲染
  像素实锤——顶部接近全透（视频透出）、底部深色（B站式"底部实、往上渐透"）。
  注：QSS background 在透明顶层窗口不渲染（实锤对照 alpha=0），渐变在
  paintEvent 手绘——"无实心黑框"以像素断言为准。
- 点3 布局：QVBoxLayout 两行——进度条独立一行（横跨整行）在上、按钮行在下
  （播放 · 下一集 · 时间 · 音量 · 倍速 · 字幕 · 全屏，对齐 B站）。
- 回归：overlay 浮现/隐藏、侧栏右缘触发/超时隐藏、mpv 穿透容错、键盘空格不变。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QVBoxLayout

_app = QApplication.instance() or QApplication([])


class _FakeCtl:
    """S7 验收测试 mock 控制器（记录控制指令调用）。"""

    def __init__(self):
        self.seeks = []
        self.toggles = 0
        self.mutes = 0
        self.plays = []
        self.playing = True
        self.pos = 60.0
        self.dur = 120.0

    def set_embed_wid(self, wid):
        pass

    def play(self, item, index=None):
        self.plays.append((item, index))
        return True

    def is_playing(self):
        return self.playing

    def stop(self):
        self.playing = False

    def toggle_pause(self):
        self.toggles += 1

    def tick(self):
        pass

    def get_position(self):
        return self.pos

    def get_duration(self):
        return self.dur

    def current_item(self):
        return None

    def is_auto_next(self):
        return True

    def set_auto_next(self, enabled):
        pass

    def seek(self, seconds, mode="relative"):
        self.seeks.append((seconds, mode))

    def set_speed(self, rate):
        pass

    def set_volume(self, vol):
        pass

    def toggle_mute(self):
        self.mutes += 1


def _mk_window():
    from app.ui.widgets.video_player_window import VideoPlayerWindow
    ctl = _FakeCtl()
    w = VideoPlayerWindow(ctl)
    return w, ctl


def _flush():
    for _ in range(3):
        _app.processEvents()


class TestTopmostOwned:  # 点1：无永久置顶 + owner 跟随播放窗口
    def test_overlay_no_permanent_topmost(self):
        """overlay 无 WindowStaysOnTopHint（不再 WS_EX_TOPMOST 全局置顶——
        别的软件盖住播放窗口时 overlay 跟随被盖）。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            assert not (ov.windowFlags() & Qt.WindowStaysOnTopHint)
        finally:
            w._timer.stop()

    def test_overlay_owned_by_playback_window(self):
        """overlay owner = 播放窗口（Qt parent；Windows owned window——
        浮在 owner 及其子窗口 mpv 之上）。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            assert ov.parent() is w
            assert ov.windowFlags() & Qt.Tool          # 顶层工具窗口保留
            assert ov.windowFlags() & Qt.FramelessWindowHint
        finally:
            w._timer.stop()

    def test_sidebar_no_permanent_topmost_owned(self):
        """侧栏（S4）同问题同修：无永久置顶 + owner = 播放窗口。"""
        w, _ = _mk_window()
        try:
            sb = w.sidebar
            assert not (sb.windowFlags() & Qt.WindowStaysOnTopHint)
            assert sb.parent() is w
            assert sb.windowFlags() & Qt.Tool
            assert sb.windowFlags() & Qt.FramelessWindowHint
        finally:
            w._timer.stop()

    def test_overlay_still_floats_above_container(self):
        """owner 关系下 overlay 仍是顶层（非容器子控件）——浮在 mpv 之上的
        机制保留（mpv 是容器 child，overlay 是 owner 的 owned window）。"""
        w, _ = _mk_window()
        try:
            assert w.overlay.parent() is w
            assert w.overlay.windowFlags() & Qt.Tool   # 独立顶层（非内嵌子控件）
        finally:
            w._timer.stop()


class TestTransparentGradient:  # 点2：渐变透明、无实心黑框
    def test_wa_translucent_background_set(self):
        """WA_TranslucentBackground 已设（顶层窗口 per-pixel alpha 前提）。"""
        w, _ = _mk_window()
        try:
            assert w.overlay.testAttribute(Qt.WA_TranslucentBackground) is True
        finally:
            w._timer.stop()

    def test_qss_no_solid_fill_no_border(self):
        """QSS：顶层显式 background:transparent（无实心 rgba 填充）+ 无黑边框
        （border:1px solid #555 已去）；按钮 hover 高亮保留。"""
        w, _ = _mk_window()
        try:
            qss = w.overlay.styleSheet()
            assert "background:transparent" in qss
            assert "border:1px solid #555" not in qss
            assert "QPushButton:hover" in qss
        finally:
            w._timer.stop()

    def test_gradient_pixels_bottom_dark_top_transparent(self):
        """渲染像素实锤：顶部接近全透（视频透出）、底部深色——B站式
        "底部实、往上渐透"（无实心黑框）。"""
        w, _ = _mk_window()
        try:
            w.resize(960, 540)
            w.show()
            w.overlay.show()
            _flush()
            img = w.overlay.grab().toImage()
            h = img.height()
            top_a = img.pixelColor(img.width() // 2, 2).alpha()
            bot_a = img.pixelColor(img.width() // 2, h - 2).alpha()
            mid_a = img.pixelColor(img.width() // 2, h // 2).alpha()
            assert top_a < 40, f"顶部应接近全透，实际 alpha={top_a}"
            assert bot_a > 180, f"底部应深色，实际 alpha={bot_a}"
            assert bot_a > mid_a > top_a, "应自下而上渐透"
        finally:
            w._timer.stop()
            w._hide_timer.stop()


class TestTwoRowLayout:  # 点3：进度条独立行 + 按钮行（对齐 B站）
    def test_vertical_two_rows(self):
        """overlay 根布局 = QVBoxLayout，恰两行：进度条行 + 按钮行。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            lay = ov.layout()
            assert isinstance(lay, QVBoxLayout)
            assert lay.count() == 2
            assert lay.itemAt(0).widget() is ov.row_progress
            assert lay.itemAt(1).widget() is ov.row_buttons
        finally:
            w._timer.stop()

    def test_progress_row_spans_full_width(self):
        """进度条独立一行且横跨整行（对齐 B站：进度条在上横跨）。"""
        w, _ = _mk_window()
        try:
            w.resize(960, 540)
            w.show()
            w.overlay.show()
            _flush()
            ov = w.overlay
            assert ov.slider_pos.parent() is ov.row_progress
            assert ov.slider_pos.geometry().width() >= ov.width() - 24
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_buttons_in_second_row_below_progress(self):
        """按钮行在进度条行之下（播放 · 下一集 · 时间 · 音量 · 倍速 · 字幕 ·
        全屏）。"""
        w, _ = _mk_window()
        try:
            w.resize(960, 540)
            w.show()
            w.overlay.show()
            _flush()
            ov = w.overlay
            for name in ("btn_toggle", "btn_next", "btn_speed", "btn_mute",
                         "btn_subtitle", "btn_fullscreen", "slider_vol"):
                assert getattr(ov, name).parent() is ov.row_buttons, name
            assert ov.lbl_time.parent() is ov.row_buttons
            assert ov.row_progress.geometry().bottom() <= \
                ov.row_buttons.geometry().top()
            # 行内顺序：播放/暂停 → 下一集 → 时间（在弹性空隙左侧）
            assert ov.btn_toggle.geometry().left() < ov.btn_next.geometry().left()
            assert ov.btn_next.geometry().right() < ov.lbl_time.geometry().left()
            assert ov.lbl_time.geometry().right() < ov.slider_vol.geometry().left()
            assert ov.slider_vol.geometry().left() < ov.btn_speed.geometry().left()
            assert ov.btn_speed.geometry().left() < ov.btn_subtitle.geometry().left()
            assert ov.btn_subtitle.geometry().left() < \
                ov.btn_fullscreen.geometry().left()
        finally:
            w._timer.stop()
            w._hide_timer.stop()


class TestRegression:  # 回归：浮现/隐藏、侧栏、穿透、键盘不变
    def test_overlay_show_hide_unchanged(self):
        """浮现/隐藏回归：_show_overlay 浮现 + 3s 计时；超时隐藏；hover 不隐藏。"""
        w, _ = _mk_window()
        try:
            w.overlay.hide()
            w._show_overlay()
            assert w.overlay.isVisible() is True
            assert w._hide_timer.isActive() is True
            w._on_overlay_hover(True)
            w._on_hide_timeout()
            assert w.overlay.isVisible() is True        # hover 中不隐藏
            w._on_overlay_hover(False)
            w._on_hide_timeout()
            assert w.overlay.isVisible() is False
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_sidebar_edge_and_timeout_unchanged(self):
        """侧栏回归：右缘触发浮现 + 3s 超时隐藏 + 停播隐藏。"""
        w, _ = _mk_window()
        try:
            item = {"vod_name": "剧", "play_url":
                    "第1集$u1#第2集$u2#第3集$u3"}
            w.play(item, index=0)
            w.notify_mouse_position(w.width() - 10)     # 右缘
            assert w.sidebar.isVisible() is True
            w._on_sidebar_hide_timeout()
            assert w.sidebar.isVisible() is False
            w._show_sidebar()
            assert w.sidebar.isVisible() is True
            w._on_stopped()
            assert w.sidebar.isVisible() is False
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_mpv_passthrough_graceful_unchanged(self, monkeypatch):
        """穿透回归：无 mpv 子窗口 → 打日志不崩（故障隔离保留）。"""
        import ctypes
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        try:
            user32 = ctypes.windll.user32
            monkeypatch.setattr(user32, "EnumChildWindows",
                                lambda hwnd, cb, _: 0)  # 无子窗口
            w._apply_mpv_mouse_passthrough()            # 不抛异常
        finally:
            w._timer.stop()

    def test_keyboard_space_unchanged(self):
        """键盘回归：空格走按钮全链（图标态切换 + toggle_pause）。"""
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        w, ctl = _mk_window()
        try:
            w.overlay._paused = False
            w.overlay.set_play_icon(False)
            ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier)
            w.keyPressEvent(ev)
            assert ctl.toggles == 1
            assert w.overlay._play_state == "pause"
        finally:
            w._timer.stop()
            w._hide_timer.stop()
            w._sidebar_hide_timer.stop()
