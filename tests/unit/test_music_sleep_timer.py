from unittest.mock import MagicMock, patch

from app.music.controller import MusicController


def _controller(tmp_path):
    cfg = {"database": str(tmp_path / "sleep.db"), "scan_dirs": [],
           "extensions": [".mp3"], "mpv_path": "mpv",
           "mpv_ipc_pipe": r"\\.\pipe\sleep-test", "default_volume": 70}
    with patch("app.music.controller.LxMusicClient") as lx:
        lx.return_value.is_connected.return_value = False
        c = MusicController(cfg, mpv_adapter=MagicMock())
    c._running = True
    c.stop_current = MagicMock(return_value=True)
    return c


def test_sleep_timer_replaces_previous_and_expires(tmp_path):
    c = _controller(tmp_path)
    assert c.set_sleep_timer(60) is True
    first = c._sleep_timer
    assert c.set_sleep_timer(60) is True
    assert not first.is_alive()
    c._sleep_timer_expired()
    c.stop_current.assert_called_once()
    assert c._sleep_timer is None


def test_sleep_timer_cancel_is_idempotent(tmp_path):
    c = _controller(tmp_path)
    assert c.set_sleep_timer(60) is True
    assert c.cancel_sleep_timer() is True
    assert c.cancel_sleep_timer() is False
    c.stop_current.assert_not_called()
