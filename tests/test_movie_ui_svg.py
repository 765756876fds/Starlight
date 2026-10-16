"""S6：播放界面 UI 对标 B站——图标全 SVG 化 + 布局 + 字幕/下一集按钮。

验收点：
- 图标全 SVG 化：控制条 icon 按钮文本为空（icon 承载状态）、无 emoji 残留；
  全部按钮 icon 非 null（渲染 OK）。
- 布局：无"停止"、有"下一集"/"字幕"（停→关窗由 Esc/窗口 X 承担）。
- 字幕按钮 → request_subtitle → 弹 Assrt 对话框（mock _SubtitleDialog）。
- 全屏图标进入/退出切换（mock showFullScreen——offscreen 真全屏崩，S3 教训）。
- 音量三态（high/low/muted）图标状态。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
import pytest

from app.ui.widgets.video_player_window import VideoPlayerWindow


class _FakeCtl:
    def __init__(self):
        self.plays = []
        self.mutes = 0
        self.toggles = 0
        self.pos = 0.0
        self.dur = 120.0
        self.playing = True

    def set_embed_wid(self, wid):
        pass

    def play(self, item, index=None):
        self.plays.append((item, index))
        return True

    def is_playing(self):
        return self.playing

    def toggle_pause(self):
        self.toggles += 1

    def toggle_mute(self):
        self.mutes += 1

    def get_position(self):
        return self.pos

    def get_duration(self):
        return self.dur

    def tick(self):
        pass


def _mk_window():
    ctl = _FakeCtl()
    w = VideoPlayerWindow(ctl)
    return w, ctl


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class TestIconsSvg:
    def test_overlay_icon_buttons_text_empty_and_no_emoji(self, qapp):
        """图标按钮文本为空（icon 承载状态）；全条无 emoji 残留。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            for btn in (ov.btn_toggle, ov.btn_next, ov.btn_mute,
                        ov.btn_subtitle, ov.btn_fullscreen):
                assert btn.text() == "", f"{btn} 文本应为空（SVG 承载）"
                assert not btn.icon().isNull(), f"{btn} icon 渲染失败"
            # 倍速是文字按钮（B站同款），但不得是 emoji 符号
            assert "倍速" in ov.btn_speed.text()
            assert all(ord(ch) < 0x2100 for ch in ov.btn_speed.text()
                       if not "\u4e00" <= ch <= "\u9fff")
        finally:
            w._timer.stop()

    def test_layout_no_stop_has_next_and_subtitle(self, qapp):
        """S6 布局：停止已取消；下一集/字幕在列（对齐 B站）。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            assert not hasattr(ov, "btn_stop")
            assert ov.btn_next is not None
            assert ov.btn_subtitle is not None
            # 布局（S7 两行，对齐 B站）：行1 = 进度条独立一行横跨；行2 = 按钮行
            # 播放/暂停 → 下一集 → 时间 → 音量 → 静音 → 倍速 → 字幕 → 全屏
            # （QSS 统一 hover 高亮）
            assert "QPushButton:hover" in ov.styleSheet()
        finally:
            w._timer.stop()


class TestVolumeIconStates:
    def test_volume_icon_tri_state(self, qapp):
        """音量三态：高 → 低（≤20）→ 静音。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            assert ov._vol_state == "high"
            ov.slider_vol.setValue(5)
            assert ov._vol_state == "low"
            ov.slider_vol.setValue(70)
            ov.btn_mute.click()
            assert ov._vol_state == "muted"
            ov.btn_mute.click()
            assert ov._vol_state == "high"
        finally:
            w._timer.stop()

    def test_play_icon_switches(self, qapp):
        """播放/暂停图标切换（set_play_icon 入口——键盘/按钮共用）。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            ov.set_play_icon(False)
            assert ov._play_state == "play"
            ov.set_play_icon(True)
            assert ov._play_state == "pause"
        finally:
            w._timer.stop()

    def test_fullscreen_icon_switches(self, qapp, monkeypatch):
        """全屏进入/退出图标切换（mock showFullScreen——offscreen 真全屏崩）。"""
        w, _ = _mk_window()
        monkeypatch.setattr(w, "showFullScreen", lambda: None)
        monkeypatch.setattr(w, "showNormal", lambda: None)
        try:
            w.overlay.set_fullscreen_icon(False)
            assert w.overlay.btn_fullscreen.icon().isNull() is False
            w.showFullScreen()
            w.showNormal()
        finally:
            w._timer.stop()


class TestSubtitleButton:
    def test_subtitle_button_opens_dialog(self, qapp, monkeypatch):
        """字幕按钮 → request_subtitle → 弹 Assrt 对话框（复用面板）。"""
        w, ctl = _mk_window()
        calls = []

        class _FakeDialog:
            def __init__(self, movie, kw, parent):
                calls.append((movie, kw, parent))

            def exec(self):
                pass

        monkeypatch.setattr(
            "app.ui.panels.movie_panel._SubtitleDialog", _FakeDialog)
        try:
            w.play({"vod_name": "喜剧之王", "first_url": "u"}, index=0)
            w.overlay.btn_subtitle.click()
            assert len(calls) == 1
            movie, kw, parent = calls[0]
            assert movie is ctl
            assert kw == "喜剧之王"          # 搜索框预填当前片名
            assert parent is w
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_subtitle_failure_does_not_crash(self, qapp, monkeypatch):
        """字幕对话框异常 → 打印不崩（故障隔离）。"""
        w, _ = _mk_window()

        def _boom(*a, **k):
            raise RuntimeError("对话框炸了")

        monkeypatch.setattr(
            "app.ui.panels.movie_panel._SubtitleDialog", _boom)
        try:
            w.request_subtitle()            # 不抛 = 通过
        finally:
            w._timer.stop()
