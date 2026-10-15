"""电影实机问题修复 4+1 项测试：
① 互斥双向暂停（不循环） ② 窗口关闭后唤醒 ③ 软件退出钩子杀电影 mpv
④ 断点续播参数（仅 video 分支） ⑤ mpv 中文轻量版（--osc=no）
"""
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.core.event_bus import Event, EventType, get_event_bus
from app.music.mpv import MpvController


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------- ② 窗口关闭后唤醒（start 守卫查进程存活） ----------

def _launched_mpv(video=False, watch_later_dir=None):
    """启动一个 MpvController（mock Popen/pipe），返回 (mpv, m_popen)。"""
    with patch("app.music.mpv.subprocess.Popen") as m_popen, \
         patch("app.music.mpv.os.path.exists", return_value=True), \
         patch("app.music.mpv.open", create=True) as m_open:
        m_open.return_value = MagicMock()
        mpv = MpvController(sys.executable, video=video,
                            watch_later_dir=watch_later_dir)
        assert mpv.start(taskkill=False, pipe_timeout=1.0, prop_timeout=0.3)
    return mpv, m_popen


def test_start_restarts_after_window_closed():
    """窗口被用户关闭（进程自死，_running 仍 True）→ start() 重新 launch。"""
    mpv, m_popen = _launched_mpv(video=True)
    assert m_popen.call_count == 1
    # 模拟窗口关闭：进程退出
    mpv._process.poll.return_value = 1
    assert mpv.is_running() is False
    # 再 start → 守卫不挡，重新 launch（唤醒）
    with patch("app.music.mpv.subprocess.Popen") as m_popen2, \
         patch("app.music.mpv.os.path.exists", return_value=True), \
         patch("app.music.mpv.open", create=True) as m_open2:
        m_open2.return_value = MagicMock()
        assert mpv.start(taskkill=False, pipe_timeout=1.0, prop_timeout=0.3)
        assert m_popen2.call_count == 1


def test_start_no_restart_when_alive():
    """进程存活 → start() 直接返回，不重复 launch（音乐链正常路径零变化）。"""
    mpv, m_popen = _launched_mpv()
    mpv._process.poll.return_value = None
    assert mpv.is_running() is True
    assert mpv.start(taskkill=False, pipe_timeout=1.0, prop_timeout=0.3) is True
    assert m_popen.call_count == 1


# ---------- ④ 断点续播参数（仅 video 分支） + ⑤ --osc=no ----------

def test_video_branch_has_osc_off_and_watch_later():
    mpv, m_popen = _launched_mpv(video=True, watch_later_dir="D:/wl/movie")
    args = m_popen.call_args[0][0]
    assert "--osc=no" in args
    assert "--save-position-on-quit" in args
    assert "--watch-later-directory=D:/wl/movie" in args
    assert "--no-video" not in args


def test_music_branch_no_watch_later_no_osc():
    """音乐链（video=False）绝对不加断点/OSC 参数（用户明确不要）。"""
    mpv, m_popen = _launched_mpv()
    args = m_popen.call_args[0][0]
    assert "--osc=no" not in args
    assert "--save-position-on-quit" not in args
    assert not any(a.startswith("--watch-later-directory") for a in args)
    assert "--no-video" in args


# ---------- MovieController 传递 watch_later_dir ----------

def _fake_cfg(overrides):
    """点分 get 风格的假配置（真 Config 支持点分，普通 dict 不支持）。"""
    cfg = MagicMock()
    base = {"app.data_dir": "./data"}
    base.update(overrides)
    cfg.get.side_effect = lambda key, default=None: base.get(key, default)
    return cfg


def test_movie_controller_passes_watch_later_dir(tmp_path):
    from app.movie.movie_controller import MovieController
    wl = str(tmp_path / "movie" / "watch_later")
    cfg = _fake_cfg({"movie.watch_later_dir": wl})
    with patch("app.movie.movie_controller.MpvController") as M:
        MovieController(config=cfg)
    kwargs = M.call_args.kwargs
    assert kwargs["video"] is True
    assert kwargs["watch_later_dir"] == wl
    assert (tmp_path / "movie" / "watch_later").is_dir()


def test_movie_controller_default_watch_later_dir(tmp_path):
    from app.movie.movie_controller import MovieController
    cfg = _fake_cfg({"app.data_dir": str(tmp_path)})
    with patch("app.movie.movie_controller.MpvController") as M:
        MovieController(config=cfg)
    kwargs = M.call_args.kwargs
    assert kwargs["watch_later_dir"] == str(tmp_path / "movie" / "watch_later")


# ---------- ③ 软件退出钩子 ----------

def test_panel_app_quit_closes_movie(qapp):
    from app.ui.panels.movie_panel import MoviePanel
    ctrl = MagicMock()
    panel = MoviePanel(movie=ctrl)
    try:
        panel._on_app_quit()
        ctrl.close.assert_called_once()
    finally:
        panel._timer.stop()


def test_panel_app_quit_idempotent_on_error(qapp):
    from app.ui.panels.movie_panel import MoviePanel
    ctrl = MagicMock()
    ctrl.close.side_effect = RuntimeError("boom")
    panel = MoviePanel(movie=ctrl)
    try:
        panel._on_app_quit()   # 不抛
    finally:
        panel._timer.stop()


# ---------- ③ 软件退出（SYSTEM_SHUTDOWN 事件收口） ----------

def _make_ctrl(**kw):
    from app.movie.movie_controller import MovieController
    with patch("app.movie.movie_controller.MpvController") as M:
        ctrl = MovieController(source_url="http://source.test/api", music=kw.pop("music", None))
        return ctrl, M.return_value


def test_system_shutdown_closes_movie():
    """main.py stop() 先 publish SYSTEM_SHUTDOWN → 电影 close() 杀 mpv（托盘退出路径覆盖）。"""
    ctrl, mmpv = _make_ctrl()
    get_event_bus().publish(Event(EventType.SYSTEM_SHUTDOWN, {}))
    mmpv.stop_process.assert_called_once()
    ctrl.close()


# ---------- ⑤ 面板增强控制按钮 ----------

def _make_panel_ctrl():
    from app.movie.movie_controller import MovieController
    with patch("app.movie.movie_controller.MpvController") as M:
        ctrl = MovieController(source_url="http://s/api")
    return ctrl, M.return_value


def test_controller_fullscreen_speed_volume_mute():
    ctrl, mmpv = _make_panel_ctrl()
    mmpv.is_running.return_value = True
    ctrl.set_fullscreen(True)
    mmpv.set_property.assert_any_call("fullscreen", True)
    ctrl.set_speed(1.5)
    mmpv.set_property.assert_any_call("speed", 1.5)
    ctrl.set_volume(42)
    mmpv.set_property.assert_any_call("volume", 42)
    mmpv.get_property.return_value = False
    ctrl.toggle_mute()
    mmpv.set_property.assert_any_call("mute", True)
    ctrl.close()


def test_controller_volume_clamped():
    ctrl, mmpv = _make_panel_ctrl()
    mmpv.is_running.return_value = True
    ctrl.set_volume(999)
    mmpv.set_property.assert_any_call("volume", 100)
    ctrl.set_volume(-5)
    mmpv.set_property.assert_any_call("volume", 0)
    ctrl.close()


def test_controller_controls_noop_when_not_running():
    """mpv 未运行 → 控制按钮静默跳过（不抛、不碰死 pipe）。"""
    ctrl, mmpv = _make_panel_ctrl()
    mmpv.is_running.return_value = False
    ctrl.set_fullscreen(True)
    ctrl.set_speed(2.0)
    ctrl.set_volume(50)
    ctrl.toggle_mute()
    mmpv.set_property.assert_not_called()
    ctrl.close()


def test_panel_speed_menu_apply(qapp):
    from app.ui.panels.movie_panel import MoviePanel
    ctrl = MagicMock()
    panel = MoviePanel(movie=ctrl)
    try:
        # 包2c：倍速改为弹菜单选档（_apply_speed 是菜单 action 的共用入口；
        # _on_speed 弹真实 QMenu 会阻塞，循环点击语义已废弃）
        assert panel.btn_speed.text() == "倍速 1x"
        panel._apply_speed(1.25)
        assert panel.btn_speed.text() == "倍速 1.25x"
        ctrl.set_speed.assert_called_once_with(1.25)
        panel._apply_speed(1.5)
        assert panel.btn_speed.text() == "倍速 1.5x"
        ctrl.set_speed.assert_called_with(1.5)
        # 菜单 9 档 + 当前档高亮由 test_movie_speed 覆盖
    finally:
        panel._timer.stop()


def test_panel_fullscreen_and_volume_buttons(qapp):
    from app.ui.panels.movie_panel import MoviePanel
    ctrl = MagicMock()
    panel = MoviePanel(movie=ctrl)
    try:
        panel._on_fullscreen()
        ctrl.set_fullscreen.assert_called_once_with(True)
        panel._on_volume(66)
        ctrl.set_volume.assert_called_once_with(66)
    finally:
        panel._timer.stop()


def test_panel_shortcut_dialog(qapp):
    from app.ui.panels.movie_panel import MoviePanel
    ctrl = MagicMock()
    panel = MoviePanel(movie=ctrl)
    try:
        with patch("app.ui.panels.movie_panel.QMessageBox.information") as m_info:
            panel._on_shortcut()
            m_info.assert_called_once()
            args = m_info.call_args[0]
            assert "空格" in args[2] and "f" in args[2] and "9" in args[2]
    finally:
        panel._timer.stop()

def _make_music_ctrl(tmp_path):
    from app.music.database import MusicDatabase
    from app.music.controller import MusicController
    db_path = str(tmp_path / "fx.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_fx",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.db = db
    return ctrl


def test_music_resume_publishes_music_play(tmp_path):
    """resume() 补发 MUSIC_PLAY → 电影可订阅（播放音乐 → 电影暂停）。"""
    bus = get_event_bus()
    got = []
    handler = lambda e: got.append(e)
    bus.subscribe(EventType.MUSIC_PLAY, handler)
    try:
        ctrl = _make_music_ctrl(tmp_path)
        ctrl.resume()
        assert len(got) == 1
        assert got[0].event_type == EventType.MUSIC_PLAY
    finally:
        bus.unsubscribe(EventType.MUSIC_PLAY, handler)


def test_music_play_continue_publishes_music_play(tmp_path):
    """play() 继续播同一首歌（非换歌）→ 补发 MUSIC_PLAY。"""
    bus = get_event_bus()
    got = []
    handler = lambda e: got.append(e)
    bus.subscribe(EventType.MUSIC_PLAY, handler)
    try:
        ctrl = _make_music_ctrl(tmp_path)
        ctrl.queue = MagicMock()
        ctrl.queue.is_empty.return_value = False
        ctrl.mpv.is_idle.return_value = False
        ctrl.mpv.get_filename.return_value = "song.mp3"
        ctrl.play()
        assert len(got) == 1
    finally:
        bus.unsubscribe(EventType.MUSIC_PLAY, handler)


# ---------- ① 修 #1：实机构造路径 + 落雪分支 + pause 单定义 ----------

def test_panel_real_construction_gets_music():
    """实机构造路径：MoviePanel(movie=None, music=...) → 内部 MovieController 拿到 music。
    上次根因：MoviePanel 不传 music → MovieController.music=None → 方向 1 完全失效。"""
    from app.ui.panels.movie_panel import MoviePanel
    fake_music = MagicMock()
    with patch("app.movie.movie_controller.MpvController"):
        panel = MoviePanel(movie=None, music=fake_music)
    try:
        assert panel._movie is not None
        assert panel._movie._music is fake_music
    finally:
        panel._timer.stop()


def test_main_tabs_real_construction_passes_music(qapp):
    """MainTabs 真实构造 → 电影面板拿到 MainTabs._music（不碰 main.py 的链路）。"""
    from app.ui.widgets.main_tabs import MainTabs
    fake_music = MagicMock()
    with patch("app.movie.movie_controller.MpvController"):
        tabs = MainTabs(
            automation_service=None, cooldown_manager=None, music=fake_music,
            ongoing_service=None, services_dict=None, snapshot_provider=None,
            history_repo=None, combo_manager=None, tts=None,
            equip_toggle=None, overlay_toggle=None, overlay_enabled=True,
        )
    movie_panel = tabs._panels.get("movie")
    assert movie_panel is not None
    assert movie_panel._movie._music is fake_music


def test_movie_pause_guard_single_definition():
    """pause() 只剩守卫版（无守卫旧定义已删）：mpv 未运行 → 不碰 mpv.pause。"""
    ctrl, mmpv = _make_ctrl()
    mmpv.is_running.return_value = False
    ctrl.pause()
    mmpv.pause.assert_not_called()
    ctrl.close()


def test_lx_play_publishes_music_play(tmp_path):
    """落雪分支播放 → 补发 MUSIC_PLAY（在线播放时电影也能暂停）。"""
    bus = get_event_bus()
    got = []
    handler = lambda e: got.append(e)
    bus.subscribe(EventType.MUSIC_PLAY, handler)
    try:
        ctrl = _make_music_ctrl(tmp_path)
        ctrl.lx_connected = True
        ctrl.lx_client = MagicMock()
        ctrl.lx_client.play.return_value = True
        ctrl.play()
        assert len(got) == 1
        assert got[0].event_type == EventType.MUSIC_PLAY
    finally:
        bus.unsubscribe(EventType.MUSIC_PLAY, handler)
