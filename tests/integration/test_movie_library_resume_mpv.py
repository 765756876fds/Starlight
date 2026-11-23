"""Read real MPV checkpoints in the new home without opening any user media."""
import time

import pytest
from PySide6.QtTest import QTest

from app.movie.library import MovieLibrary
from tests.integration.test_movie_resume_mpv import media, sessions, qapp

pytestmark = pytest.mark.integration


def test_home_continuing_reads_native_checkpoint_without_launching_another_player(sessions, media):
    session = sessions()
    session.play(media[1])
    # The shared fixture pauses once position is readable. The home includes
    # only confirmed starts, so observe normal playback before making a seek.
    assert session.mpv.set_property("pause", False)["error"] == "success"
    deadline = time.monotonic() + 5
    while session.ctl.playback_startup().state == "accepted" and time.monotonic() < deadline:
        session.ctl.tick()
        QTest.qWait(50)
    assert session.ctl.playback_startup().state == "started"

    session.seek(17.5)
    session.ctl.tick()  # Same polling entry as MoviePanel, with auto-next off.
    assert session.ctl.stop() is True
    process = session.mpv._process
    history = session.ctl.get_history()
    entries = MovieLibrary.continuing(history, session.watch)
    assert len(entries) == 1
    assert entries[0]["resume_seconds"] == pytest.approx(17.5, abs=.4)
    assert entries[0]["replay_value"] == str(media[1])
    assert session.mpv._process is process
    assert session.mpv.get_property("idle-active", timeout=.3) is True
