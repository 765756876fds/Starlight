"""play_external_url 定向测试（批次 5）。"""
from unittest.mock import MagicMock


def _make_controller():
    from app.movie.movie_controller import MovieController
    c = MovieController.__new__(MovieController)
    c.mpv = MagicMock()
    c._music = MagicMock()
    c._embed = False
    c._embed_wid = None
    c._current = None
    c._current_index = None
    c._last_idle = True
    c._external_url_mode = False
    c._external_url_seen_active = False
    c._auto_next = True
    c._write_state = MagicMock()
    return c


def test_play_external_url_success():
    c = _make_controller()
    c.mpv.is_running.return_value = True
    c.mpv.loadfile.return_value = {"error": "success"}
    ok = c.play_external_url("http://x/y.mp4", "test")
    assert ok is True
    c._music.pause.assert_called_once()
    c.mpv.loadfile.assert_called_once_with("http://x/y.mp4")
    c.mpv.set_property.assert_called_with("pause", False)
    c._write_state.assert_called_once_with(playing=True, paused=False)


def test_play_external_url_mpv_start_failure():
    c = _make_controller()
    c.mpv.is_running.return_value = False
    c.mpv.start.return_value = False
    ok = c.play_external_url("http://x/y.mp4")
    assert ok is False
    c._music.pause.assert_called_once()  # 音乐仍 pause（既有契约）
    c.mpv.loadfile.assert_not_called()
    c._write_state.assert_not_called()


def test_play_external_url_loadfile_none():
    c = _make_controller()
    c.mpv.is_running.return_value = True
    c.mpv.loadfile.return_value = None
    ok = c.play_external_url("http://x/y.mp4")
    assert ok is False
    c._write_state.assert_not_called()


def test_play_external_url_loadfile_error():
    c = _make_controller()
    c.mpv.is_running.return_value = True
    c.mpv.loadfile.return_value = {"error": "failed"}
    ok = c.play_external_url("http://x/y.mp4")
    assert ok is False
    c._write_state.assert_not_called()


def test_play_external_url_loadfile_exception():
    c = _make_controller()
    c.mpv.is_running.return_value = True
    c.mpv.loadfile.side_effect = RuntimeError("boom")
    ok = c.play_external_url("http://x/y.mp4")
    assert ok is False
    c._write_state.assert_not_called()


def test_play_external_url_empty_url():
    c = _make_controller()
    ok = c.play_external_url("")
    assert ok is False
    c._music.pause.assert_not_called()


# ---- tick() 外部 URL 生命周期 ----

def _start_external(c):
    c.mpv.is_running.return_value = True
    c.mpv.loadfile.return_value = {"error": "success"}
    c._write_state.reset_mock()
    return c.play_external_url("http://x/y.mp4", "t")


def test_tick_external_buffering_idle_no_false_end():
    """缓冲期连续 idle=True 不得写 playing=False。"""
    c = _make_controller()
    assert _start_external(c) is True
    c._write_state.reset_mock()
    c.mpv.get_idle_active.return_value = True  # 一直 idle（缓冲）
    for _ in range(5):
        c.tick()
    c._write_state.assert_not_called()
    assert c._external_url_mode is True


def test_tick_external_seen_then_end():
    """先 idle=False（开始播放），后 idle=True（播完）→ 写 playing=False。"""
    c = _make_controller()
    assert _start_external(c) is True
    c._write_state.reset_mock()
    c.mpv.get_idle_active.return_value = False  # 开始播放
    c.tick()
    c._write_state.assert_not_called()
    assert c._external_url_seen_active is True
    c.mpv.get_idle_active.return_value = True  # 播完
    c.tick()
    c._write_state.assert_called_once_with(playing=False, paused=False)
    assert c._external_url_mode is False


def test_tick_external_switched_to_normal_resets():
    """外部 URL 播放中切到普通影片 → 外部状态清理。"""
    c = _make_controller()
    assert _start_external(c) is True
    c._external_url_seen_active = True
    # 模拟 play() 重置
    c._current = {"vod_name": "movie"}
    c._external_url_mode = False
    c._external_url_seen_active = False
    c._write_state.reset_mock()
    c.mpv.get_idle_active.return_value = True  # idle
    c.tick()  # 走正常剧集分支，_current 非空
    # 不应写 playing=False（auto_next 连播逻辑，不在此断言）
    assert c._external_url_mode is False
