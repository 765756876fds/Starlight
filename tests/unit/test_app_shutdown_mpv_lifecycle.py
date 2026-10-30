"""应用退出时显式关闭懒加载的电影播放器。"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _app_with_movie(movie, music=None):
    from main import Application

    app = Application.__new__(Application)
    app._running = True
    app.event_bus = MagicMock()
    app.voice = None
    app.speech_judge = None
    app.speech_queue = None
    app.tts = None
    app.lol = None
    app.automation = None
    app.music = music
    app._movie_panel = SimpleNamespace(_movie=movie)
    app._asr_legacy_proc = None
    app.combo_manager = None
    app.scheduler = MagicMock()
    app.state_store = MagicMock()
    app.event_log = None
    return app


def test_application_stop_closes_movie_controller():
    movie = MagicMock()
    app = _app_with_movie(movie)

    with patch("app.core.log_redirect.close_redirect"):
        app.stop()

    movie.close.assert_called_once_with()


def test_movie_cleanup_failure_does_not_block_music_cleanup():
    movie = MagicMock()
    movie.close.side_effect = RuntimeError("movie cleanup failed")
    music = MagicMock()
    app = _app_with_movie(movie, music=music)

    with patch("app.core.log_redirect.close_redirect"):
        app.stop()

    music.stop.assert_called_once_with()
