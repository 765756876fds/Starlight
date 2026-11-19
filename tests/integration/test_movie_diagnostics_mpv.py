"""Owned generated media/MPV only; --vo=null does not prove DWM rendering."""
import threading
import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import QTimer

from app.movie.playback_diagnostics import read_sample
from tests.integration.test_movie_resume_mpv import (
    media as media, qapp as qapp, sessions as sessions,
)

pytestmark = pytest.mark.integration


def test_readonly_ipc_independent_of_command_lock_and_qt_responsive(sessions, media, qapp):
    session = sessions("independent-diagnostics")
    session.play(media[1])
    result, done = {}, threading.Event()
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    def observe():
        result.update(read_sample(session.mpv.ipc_pipe))
        done.set()
    worker = threading.Thread(target=observe)
    try:
        # Even a busy command pipe must not block our independent read-only probe.
        with session.mpv._pipe_lock:
            started = time.monotonic()
            worker.start()
            while not done.is_set() and time.monotonic() - started < 1:
                qapp.processEvents()
                time.sleep(.005)
        worker.join(1)
        assert done.is_set() and not worker.is_alive()
        assert time.monotonic() - started < .6
        assert result["pause"] is True
        assert type(result["time-pos"]) in (int, float)
        assert "path" not in str(result) and str(media[1]) not in str(result)
        # Probe is fast enough that one sample may finish before the first timer;
        # explicitly keep pumping the same live GUI for 50ms.
        end = time.monotonic() + .05
        while time.monotonic() < end:
            qapp.processEvents()
            time.sleep(.005)
        assert len(ticks) >= 3
    finally:
        timer.stop()
        session.ctl.close()
        worker.join(1)


def test_opt_in_monitor_does_not_reload_or_unpause_and_closes(sessions, media):
    session = sessions("diagnostic-lifecycle")
    session.play(media[1])
    assert not hasattr(session.ctl, "_playback_monitor")  # default remains off
    original_get = session.ctl._config.get
    session.ctl._config.get = lambda key, default=None: (
        True if key == "movie.playback_diagnostics" else original_get(key, default)
    )
    messages = []
    try:
        identity = session.mpv.get_property("playlist/0/id")
        with patch("app.movie.movie_controller.logger.info", side_effect=messages.append):
            session.ctl._start_playback_diagnostics("local")
            monitor = session.ctl._playback_monitor
            deadline = time.monotonic() + 1
            while not messages and time.monotonic() < deadline:
                time.sleep(.01)
            assert any("[MoviePerf]" in msg for msg in messages)
            assert all(str(media[1]) not in msg for msg in messages)
            assert session.mpv.get_property("pause") is True
            assert session.mpv.get_property("playlist/0/id") == identity
            session.ctl.close()
            monitor._thread.join(1)
            assert not monitor._thread.is_alive()
            assert not session.mpv.is_running()
    finally:
        session.ctl.close()


def test_real_samples_track_pause_seek_and_episode_switch(sessions, media):
    session = sessions("read-only-controls")
    try:
        session.play(media[1])
        assert read_sample(session.mpv.ipc_pipe)["pause"] is True
        session.seek(8)
        assert read_sample(session.mpv.ipc_pipe)["time-pos"] == pytest.approx(8, abs=.5)
        old_generation = session.ctl._playback_gen
        session.play(media[2])
        assert session.ctl._playback_gen > old_generation
        assert read_sample(session.mpv.ipc_pipe)["time-pos"] == pytest.approx(0, abs=.5)
        assert session.ctl.stop() is True
        assert read_sample(session.mpv.ipc_pipe) == {}
    finally:
        session.ctl.close()
