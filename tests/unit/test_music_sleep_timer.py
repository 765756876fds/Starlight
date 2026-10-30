from unittest.mock import MagicMock, patch

from app.music.controller import MusicController
from app.core.event_bus import EventType


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


def test_sleep_timer_state_events_cover_set_and_expiry(tmp_path):
    c = _controller(tmp_path)
    events = []
    c.event_bus.subscribe(EventType.MUSIC_STATE_CHANGED, events.append)
    assert c.set_sleep_timer(1800) is True
    c._sleep_timer_expired()
    assert [(e.data["active"], e.data["seconds"]) for e in events
            if e.data.get("state") == "sleep_timer"] == [(True, 1800), (False, 0)]


def test_play_returns_failure_when_local_library_empty(tmp_path):
    c = _controller(tmp_path)
    c._mpv_available = True
    c.db.get_random_songs = MagicMock(return_value=[])
    assert c.play() is False


def test_play_selects_local_song_when_queue_empty(tmp_path):
    c = _controller(tmp_path)
    c._mpv_available = True
    song = {"id": 1, "title": "本地歌", "path": "C:/song.mp3"}
    c.db.get_random_songs = MagicMock(return_value=[song])
    c._play_song = MagicMock()
    assert c.play() is True
    c._play_song.assert_called_once_with(song)


def test_sleep_timer_cancel_is_idempotent(tmp_path):
    c = _controller(tmp_path)
    assert c.set_sleep_timer(60) is True
    assert c.cancel_sleep_timer() is True
    assert c.cancel_sleep_timer() is False
    c.stop_current.assert_not_called()
