"""电影内嵌播放 S0+S1 测试：
- mpv --wid 参数化（音乐链零影响——硬指标）
- MovieController embed 开关 + wid 透传（play 走内嵌/回退独立）
- VideoPlayerWindow 结构/关窗=停播/Esc/attach_current（offscreen）
- MoviePanel embed 分支（_play_item 走播放窗口 + stopped 信号清状态）
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest

from app.music.mpv import MpvController
from app.movie.movie_controller import MovieController


# ---------- mpv --wid 参数化 ----------

def _patch_launch():
    return patch.object(MpvController, "_wait_for_pipe", return_value=None)


def _mk_mpv(tmp_path, video):
    fake = str(tmp_path / "mpv.exe")
    open(fake, "w").close()
    return MpvController(fake, r"\\.\pipe\embed_test", video=video)


def _args(popen_mock):
    return popen_mock.call_args.args[0]


class TestMpvWid:
    def test_video_start_with_wid_adds_flag(self, tmp_path):
        """video 实例 + wid → args 含 --wid（uint32 转换）。"""
        ctl = _mk_mpv(tmp_path, video=True)
        with patch("app.music.mpv.subprocess.Popen") as popen, \
             _patch_launch(), \
             patch.object(MpvController, "_init_properties"):
            ok = ctl.start(wid=0x1A2B)
        assert ok is True
        args = _args(popen)
        assert "--wid=6699" in args  # 0x1A2B = 6699

    def test_video_start_without_wid_omits_flag(self, tmp_path):
        """video 实例 + 无 wid → args 无 --wid（独立窗口现状）。"""
        ctl = _mk_mpv(tmp_path, video=True)
        with patch("app.music.mpv.subprocess.Popen") as popen, \
             _patch_launch(), \
             patch.object(MpvController, "_init_properties"):
            ok = ctl.start()
        assert ok is True
        assert all(not a.startswith("--wid=") for a in _args(popen))

    def test_music_start_never_gets_wid(self, tmp_path):
        """音乐链（video=False）即使传 wid 也不加 --wid——音乐链零影响（硬指标）。"""
        ctl = _mk_mpv(tmp_path, video=False)
        with patch("app.music.mpv.subprocess.Popen") as popen, \
             _patch_launch(), \
             patch.object(MpvController, "_init_properties"):
            ok = ctl.start(wid=123)
        assert ok is True
        args = _args(popen)
        assert "--no-video" in args
        assert all(not a.startswith("--wid=") for a in args)

    def test_negative_hwnd_coerced_to_uint32(self, tmp_path):
        """win32 HWND 高位为负 → 转 uint32（负值 mpv 拒收）。"""
        ctl = _mk_mpv(tmp_path, video=True)
        with patch("app.music.mpv.subprocess.Popen") as popen, \
             _patch_launch(), \
             patch.object(MpvController, "_init_properties"):
            ok = ctl.start(wid=0xFFFFFFFF)
        assert ok is True
        assert "--wid=4294967295" in _args(popen)


# ---------- MovieController embed 开关 + wid 透传 ----------

def _mk_ctrl(config=None):
    with patch("app.movie.movie_controller.MpvController") as M:
        ctrl = MovieController(source_url="http://source.test/api", config=config)
        return ctrl, M.return_value


class _Cfg:
    """movie.embed 可控的 config stub。"""

    def __init__(self, embed):
        self._embed = embed

    def get(self, key, default=None):
        if key == "movie.embed":
            return self._embed
        return default


_ITEM = {"vod_name": "流浪地球2", "first_url": "https://a/i.m3u8"}


class TestControllerEmbed:
    def test_embed_default_true(self):
        """默认 embed 开（settings 未配置时也默认 true——设计安全阀）。"""
        with patch("app.movie.movie_controller.MpvController") as M:
            ctrl = MovieController(source_url="http://source.test/api")
        assert ctrl.embed_enabled() is True
        ctrl.close()

    def test_embed_off_via_config(self):
        """movie.embed=false → embed_enabled False（回退独立窗口路径）。"""
        ctrl, _ = _mk_ctrl(config=_Cfg(False))
        assert ctrl.embed_enabled() is False
        ctrl.close()

    def test_play_embed_wid_passed_to_mpv(self):
        """embed 开 + 窗口注入 winId → play 透传 wid 到 mpv.start（内嵌渲染）。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        mpv.start.return_value = True
        ctrl.set_embed_wid(0x1A2B)
        ok = ctrl.play(_ITEM)
        assert ok is True
        mpv.start.assert_called_once_with(taskkill=False, wid=0x1A2B)
        ctrl.close()

    def test_play_embed_no_wid_falls_back(self):
        """embed 开但窗口未注入 winId → 独立窗口（无 --wid，安全回退）。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = False
        mpv.start.return_value = True
        ok = ctrl.play(_ITEM)
        assert ok is True
        mpv.start.assert_called_once_with(taskkill=False, wid=None)
        ctrl.close()

    def test_play_embed_off_no_wid(self):
        """embed 关 → 即使有注入 wid 也不传（独立窗口现状）。"""
        ctrl, mpv = _mk_ctrl(config=_Cfg(False))
        mpv.is_running.return_value = False
        mpv.start.return_value = True
        ctrl.set_embed_wid(0x1A2B)
        ok = ctrl.play(_ITEM)
        assert ok is True
        mpv.start.assert_called_once_with(taskkill=False, wid=None)
        ctrl.close()

    def test_set_embed_wid_roundtrip(self):
        ctrl, _ = _mk_ctrl()
        ctrl.set_embed_wid(42)
        assert ctrl.get_embed_wid() == 42
        ctrl.set_embed_wid(None)
        assert ctrl.get_embed_wid() is None
        ctrl.close()

    def test_play_mpv_running_reuses_no_wid_change(self):
        """mpv 已在运行（复用不重启）→ 不调 start（wid 不变，现状语义）。"""
        ctrl, mpv = _mk_ctrl()
        mpv.is_running.return_value = True
        ctrl.set_embed_wid(0x1A2B)
        ok = ctrl.play(_ITEM)
        assert ok is True
        mpv.start.assert_not_called()
        mpv.loadfile.assert_called_once()
        ctrl.close()


# ---------- VideoPlayerWindow（offscreen） ----------

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])


class _FakeCtl:
    """播放窗口测试用 mock 控制器。"""

    def __init__(self):
        self.wid = None
        self.play_ok = True
        self.stopped = 0
        self.playing = False

    def set_embed_wid(self, wid):
        self.wid = wid

    def play(self, item, index=None):
        return self.play_ok

    def is_playing(self):
        return self.playing

    def stop(self):
        self.stopped += 1
        self.playing = False

    def toggle_pause(self):
        pass

    def tick(self):
        pass

    def get_position(self):
        return 0.0

    def get_duration(self):
        return 0.0

    def current_item(self):
        return None

    def is_auto_next(self):
        return True

    def set_auto_next(self, enabled):
        pass


class TestVideoPlayerWindow:
    def test_structure_and_native_id(self):
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        try:
            assert w.container is not None
            assert w.overlay.windowFlags() & Qt.WindowStaysOnTopHint  # 独立顶层：不被 mpv 盖
            assert w.overlay.windowFlags() & Qt.Tool
            assert isinstance(w.container.native_id(), int)
            assert w.container.native_id() == w.container.native_id()  # 缓存
        finally:
            w._timer.stop()

    def test_play_injects_wid_and_shows_overlay(self):
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        try:
            ok = w.play({"vod_name": "流浪地球2", "first_url": "https://a/i.m3u8"})
            assert ok is True
            assert ctl.wid is not None          # 容器 winId 注入控制器
            assert w._playing is True
            assert w.overlay.isVisible() is True
            assert w._timer.isActive() is True
        finally:
            w._timer.stop()

    def test_play_fail_no_overlay(self):
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        ctl.play_ok = False
        w = VideoPlayerWindow(ctl)
        try:
            ok = w.play({"vod_name": "X", "first_url": "u"})
            assert ok is False
            assert w._playing is False
            assert w.overlay.isVisible() is False
        finally:
            w._timer.stop()

    def test_close_stops_and_hides(self):
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        w.show()
        try:
            w.play({"vod_name": "X", "first_url": "u"})
            w.close()
            assert ctl.stopped == 1           # 关窗 = 停播
            assert w.isVisible() is False      # hide 保留 winId（复用）
            assert w._playing is False
            assert w.overlay.isVisible() is False
        finally:
            w._timer.stop()

    def test_escape_closes_window(self):
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        from PySide6.QtTest import QTest
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        w.show()
        try:
            w.play({"vod_name": "X", "first_url": "u"})
            QTest.keyClick(w, Qt.Key_Escape)   # 非全屏 → 关窗
            assert ctl.stopped == 1
            assert w.isVisible() is False
        finally:
            w._timer.stop()

    def test_attach_current_external_play(self):
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        ctl.playing = True                     # 语音路径：mpv 已内嵌开播
        w = VideoPlayerWindow(ctl)
        try:
            w.attach_current()
            assert w._playing is True
            assert w.overlay.isVisible() is True
            assert w._timer.isActive() is True
        finally:
            w._timer.stop()

    def test_stopped_signal_on_quit(self):
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        fired = []
        w.stopped.connect(lambda: fired.append(1))
        try:
            w.play({"vod_name": "X", "first_url": "u"})
            w.close()
            assert fired == [1]
        finally:
            w._timer.stop()

    def test_tick_runs_autonext_before_stopped_check(self):
        """播完一集 → tick 先跑连播（下一集 is_playing 恢复 True）→ 窗口不停轮询。
        若先查 is_playing 再 tick，播完瞬间误判停播、连播永远起不来（回归防护）。"""
        from app.ui.widgets.video_player_window import VideoPlayerWindow

        class _AutoNextCtl(_FakeCtl):
            def __init__(self):
                super().__init__()
                self.ticks = 0

            def tick(self):
                self.ticks += 1
                self.playing = True   # 连播把状态恢复

            def is_playing(self):
                return self.playing

        ctl = _AutoNextCtl()
        w = VideoPlayerWindow(ctl)
        try:
            w.play({"vod_name": "X", "first_url": "u"})
            w._tick()
            assert ctl.ticks == 1
            assert w._playing is True        # 未误判停播
            assert w.overlay.isVisible() is True
        finally:
            w._timer.stop()


# ---------- MoviePanel embed 分支 ----------

import pytest

from app.ui.panels.movie_panel import MoviePanel


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _PanelFakeCtl(_FakeCtl):
    """面板测试用：embed_enabled True + 播放计数。"""

    def __init__(self):
        super().__init__()
        self.plays = []

    def embed_enabled(self):
        return True

    def play(self, item, index=None):
        self.plays.append(item)
        return True


class TestPanelEmbed:
    def test_panel_builds_hidden_window_when_embed(self, qapp):
        """embed 开 → 面板懒建隐藏播放窗口并注入 winId（语音首播也内嵌）。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        try:
            assert panel._video_win is not None
            assert fake.wid is not None
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_panel_play_item_goes_embed_window(self, qapp):
        """_play_item → 播放窗口 play（呈现层接管）+ 面板标 embed 播放态。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        try:
            it = {"vod_name": "流浪地球2", "first_url": "https://a/i.m3u8"}
            panel._play_item(it)
            assert fake.plays == [it]
            assert panel._embed_playing is True
            assert panel.lbl_now.text().startswith("播放中")
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_panel_video_stopped_clears_state(self, qapp):
        """播放窗口 stopped 信号 → 面板清 embed 播放态（连播/进度让位窗口）。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        try:
            it = {"vod_name": "流浪地球2", "first_url": "https://a/i.m3u8"}
            panel._play_item(it)
            assert panel._embed_playing is True
            panel._video_win.close()          # 关窗 → stopped → 面板清态
            assert panel._embed_playing is False
            assert panel.lbl_now.text() == "未播放"
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_panel_tick_skipped_while_embed_playing(self, qapp):
        """embed 播放中面板轮询让位（连播/进度唯一驱动在播放窗口，防双 tick）。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        try:
            it = {"vod_name": "流浪地球2", "first_url": "https://a/i.m3u8"}
            panel._play_item(it)
            assert panel._embed_playing is True
            before = fake.stopped
            panel._tick()                     # embed 播放中 → 直接 return
            assert fake.stopped == before
            assert panel.lbl_res.text() == ""  # 未刷新（窗口驱动）
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()
