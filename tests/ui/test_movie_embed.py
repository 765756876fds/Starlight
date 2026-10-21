"""电影内嵌播放 S0+S1 测试：
- mpv --wid 参数化（音乐链零影响——硬指标）
- MovieController embed 开关 + wid 透传（play 走内嵌/回退独立）
- VideoPlayerWindow 结构/关窗=停播/Esc/attach_current（offscreen）
- MoviePanel embed 分支（_play_item 走播放窗口 + stopped 信号清状态）
"""
import os
import ctypes

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest
pytestmark = pytest.mark.ui


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
            # S7：去永久置顶（owner = 播放窗口——被盖跟随；仍浮在 mpv 之上）
            assert w.overlay.windowFlags() & Qt.Tool
            assert not (w.overlay.windowFlags() & Qt.WindowStaysOnTopHint)
            assert w.overlay.parent() is w
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


def _flush(qapp):
    """触发 singleShot(0) 延后回调（processEvents 处理 zero-timer；多拍防宿主抖动）。"""
    for _ in range(3):
        qapp.processEvents()


class _PanelFakeCtl(_FakeCtl):
    """面板测试用：embed_enabled True + 播放计数。"""

    def __init__(self):
        super().__init__()
        self.plays = []

    def embed_enabled(self):
        return True

    def play(self, item, index=None):
        self.plays.append(item)
        return self.play_ok


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
            _flush(qapp)                       # singleShot(0) 延后回调
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
            _flush(qapp)
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
            _flush(qapp)
            assert panel._embed_playing is True
            before = fake.stopped
            panel._tick()                     # embed 播放中 → 直接 return
            assert fake.stopped == before
            assert panel.lbl_res.text() == ""  # 未刷新（窗口驱动）
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_panel_fullscreen_embed_goes_window(self, qapp):
        """embed 播放中面板"全屏"→ 播放窗口 Qt 全屏（mpv set_fullscreen 内嵌无效）。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        try:
            it = {"vod_name": "流浪地球2", "first_url": "https://a/i.m3u8"}
            panel._play_item(it)
            _flush(qapp)
            panel._video_win.show()
            panel._on_fullscreen()
            assert panel._video_win._fullscreen is True
            assert panel._video_win.isFullScreen() is True
            panel._video_win.showNormal()
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()


class TestPanelEmbedDeferred:
    """P0 卡死修复：显示序列延后一拍（singleShot）+ 删 raise_/activateWindow。"""

    def test_play_item_deferred_until_events(self, qapp):
        """_play_item → 不立即 play（当前 slot 先返回，宿主消息泵喘息）；
        processEvents 后延后回调执行 play。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        try:
            it = {"vod_name": "X", "first_url": "u"}
            panel._play_item(it)
            assert fake.plays == []            # 延后：未同步执行
            assert panel._embed_playing is False
            _flush(qapp)
            assert fake.plays == [it]
            assert panel._embed_playing is True
            assert panel.lbl_now.text().startswith("播放中")
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_play_item_never_calls_raise_or_activate(self, qapp):
        """延后路径不再直接调 raise_()/activateWindow()（宿主 z-order/前台锁高危）。
        用实例方法包装记录（PySide6 builtin 方法 patch.object 不可靠）。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        raised, activated = [], []
        panel._video_win.raise_ = lambda: raised.append(1)
        panel._video_win.activateWindow = lambda: activated.append(1)
        try:
            it = {"vod_name": "X", "first_url": "u"}
            panel._play_item(it)
            _flush(qapp)
            assert raised == []                # 全程零调用
            assert activated == []
            assert fake.plays == [it]          # play 正常执行
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_voice_feedback_deferred_attach(self, qapp):
        """语音"开始播放" → 延后 show+attach（不立即）；不调 raise_/activateWindow。"""
        fake = _PanelFakeCtl()
        fake.playing = True                    # 语音路径：mpv 已内嵌开播
        panel = MoviePanel(movie=fake)
        raised, activated = [], []
        panel._video_win.raise_ = lambda: raised.append(1)
        panel._video_win.activateWindow = lambda: activated.append(1)
        try:
            panel._on_voice_feedback("开始播放 流浪地球2")
            assert panel._embed_playing is False   # 延后
            _flush(qapp)
            assert panel._embed_playing is True    # attach 接管
            assert raised == []
            assert activated == []
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_embed_play_fail_shows_status(self, qapp):
        """延后 play 失败 → 状态栏"无法播放"（同步语义保留，不丢反馈）。"""
        fake = _PanelFakeCtl()
        fake.play_ok = False
        panel = MoviePanel(movie=fake)
        try:
            it = {"vod_name": "X", "first_url": "u"}
            panel._play_item(it)
            _flush(qapp)
            assert panel.lbl_status.text() == "无法播放：无可用地址"
            assert panel._embed_playing is False
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_non_embed_play_item_stays_sync(self, qapp):
        """embed 关 → _play_item 走面板直控（同步 play，不受延后影响——现状语义）。"""
        fake = _PanelFakeCtl()
        fake.embed_enabled = lambda: False
        panel = MoviePanel(movie=fake)
        try:
            assert panel._video_win is None
            it = {"vod_name": "X", "first_url": "u"}
            panel._play_item(it)
            assert fake.plays == [it]          # 同步（非 embed 现状）
            assert panel._embed_playing is False
        finally:
            panel._timer.stop()


class TestTopmostRestore:
    """宿主盖窗口修复：SetWindowPos 异步置顶（替代 Qt raise_，
    只动 z-order 不碰前台锁——不触发跨进程激活死锁）。"""

    def test_raise_topmost_calls_setwindowpos(self, qapp):
        """raise_topmost → SetWindowPos(hwnd, HWND_TOP=0,
        SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE=0x13)。
        HWND_TOP（同级置顶）非 HWND_TOPMOST（全局永久置顶——实机发现压着
        一切软件、Alt+Tab 切不走）。"""
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        try:
            with patch.object(ctypes.windll.user32, "SetWindowPos") as swp:
                w.raise_topmost()
            assert swp.call_count == 1
            args = swp.call_args.args
            # c_void_p(0) = NULL 指针 → value None（等价 HWND_TOP=0）
            assert args[1].value is None                     # HWND_TOP
            assert args[6] == 0x0002 | 0x0001 | 0x0010       # NOMOVE|NOSIZE|NOACTIVATE
            assert args[2:6] == (0, 0, 0, 0)                 # 只动 z-order
        finally:
            w._timer.stop()

    def test_raise_topmost_argtypes_not_truncated(self, qapp):
        """argtypes 用 c_void_p（64 位 HWND 不截断——c_int 截断静默失败，之前踩过）。"""
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        try:
            with patch.object(ctypes.windll.user32, "SetWindowPos") as swp:
                w.raise_topmost()
            at = swp.argtypes
            assert at[0] is ctypes.c_void_p                  # hWnd
            assert at[1] is ctypes.c_void_p                  # hWndInsertAfter
        finally:
            w._timer.stop()

    def test_do_embed_play_queues_topmost(self, qapp):
        """_play_item（embed）→ 延后回调 show+play + 排队 raise_topmost（不直接调 Qt raise_）。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        tops = []
        panel._video_win.raise_topmost = lambda: tops.append(1)
        try:
            it = {"vod_name": "X", "first_url": "u"}
            panel._play_item(it)
            _flush(qapp)
            assert fake.plays == [it]           # 播放正常
            assert tops == [1]                  # SetWindowPos 置顶已排队执行
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_do_embed_attach_queues_topmost(self, qapp):
        """语音路径：延后 show+attach + 排队 raise_topmost。"""
        fake = _PanelFakeCtl()
        fake.playing = True
        panel = MoviePanel(movie=fake)
        tops = []
        panel._video_win.raise_topmost = lambda: tops.append(1)
        try:
            panel._on_voice_feedback("开始播放 X")
            _flush(qapp)
            assert panel._embed_playing is True
            assert tops == [1]
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()


class TestVoiceFeedbackWindow:
    """语音"开始播放"→ 播放窗口接管（对齐手动路径 + 懒建兜底）。"""

    def test_voice_feedback_start_play_shows_window(self, qapp):
        """语音"开始播放"→ singleShot 接管 → 窗口 show + attach + _embed_playing。"""
        fake = _PanelFakeCtl()
        fake.playing = True
        panel = MoviePanel(movie=fake)
        try:
            panel._on_voice_feedback("开始播放：喜剧之王")
            _flush(qapp)
            assert panel._embed_playing is True
            assert panel._video_win is not None
            assert panel._video_win.isVisible() is True
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_voice_feedback_lazily_builds_missing_window(self, qapp):
        """窗口缺失（任何原因未建）→ 语音"开始播放"懒建兜底 → 照常弹窗（修复核心）。"""
        fake = _PanelFakeCtl()
        fake.playing = True
        panel = MoviePanel(movie=fake)
        try:
            panel._video_win = None          # 模拟窗口未建（防御场景）
            panel._on_voice_feedback("开始播放：喜剧之王")
            _flush(qapp)
            assert panel._video_win is not None      # 懒建补上
            assert panel._embed_playing is True
            assert panel._video_win.isVisible() is True
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_voice_feedback_not_found_does_not_pop(self, qapp):
        """"未找到"等非播放反馈 → 不弹窗（状态栏只显示文案）。"""
        fake = _PanelFakeCtl()
        panel = MoviePanel(movie=fake)
        try:
            panel._on_voice_feedback("未找到：XX")
            _flush(qapp)
            assert panel._embed_playing is False
            assert panel._video_win.isVisible() is False
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_voice_feedback_embed_off_keeps_panel_path(self, qapp):
        """embed 关 → 语音"开始播放"不建窗口（面板直控现状，零回归）。"""
        fake = _PanelFakeCtl()
        fake.embed_enabled = lambda: False
        panel = MoviePanel(movie=fake)
        try:
            assert panel._video_win is None
            panel._on_voice_feedback("开始播放：喜剧之王")
            _flush(qapp)
            assert panel._video_win is None
            assert panel._embed_playing is False
        finally:
            panel._timer.stop()

    def test_overlay_buttons_have_chinese_tooltips(self, qapp):
        """控制条按钮中文 tooltip（S6 布局：播放/下一集/倍速/静音/字幕/全屏）。"""
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        try:
            assert w.overlay.btn_toggle.toolTip() == "暂停/继续"
            assert w.overlay.btn_next.toolTip() == "下一集"
            assert w.overlay.btn_speed.toolTip() == "倍速（9 档）"
            assert w.overlay.btn_mute.toolTip() == "静音"
            assert w.overlay.btn_subtitle.toolTip() == "字幕"
            assert w.overlay.btn_fullscreen.toolTip() == "全屏"
        finally:
            w._timer.stop()

    def test_voice_feedback_uses_signal_not_direct_timer(self, qapp):
        """语音接管经 _voice_attach_requested 信号投递主线程（跨线程安全）——
        语音命令在工作线程 execute_async，singleShot 从工作线程排队永不执行（根因回归防护）。"""
        fake = _PanelFakeCtl()
        fake.playing = True
        panel = MoviePanel(movie=fake)
        attached = []
        panel._video_win.attach_current = lambda: attached.append(1)
        try:
            panel._on_voice_feedback("开始播放：喜剧之王")
            _flush(qapp)
            assert attached == [1]          # 信号 → 槽 → singleShot → attach 全链
        finally:
            panel._timer.stop()
            if panel._video_win is not None:
                panel._video_win._timer.stop()

    def test_tick_tolerates_none_focus_handles(self, qapp):
        """GetFocus/GetForegroundWindow 返回 None（c_void_p NULL）→ _tick 不炸格式串
        （int(f or 0) 兜底；修复 [TEMP-DIAG tick] err: unsupported format string）。"""
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        ctl.playing = True
        w = VideoPlayerWindow(ctl)
        try:
            import ctypes
            with patch.object(ctypes.windll.user32, "GetFocus", return_value=None), \
                 patch.object(ctypes.windll.user32, "GetForegroundWindow", return_value=None):
                w._diag_n = 9        # 下一次 tick 触发诊断分支
                w._tick()            # 不应抛（格式串兜底）
        finally:
            w._timer.stop()
