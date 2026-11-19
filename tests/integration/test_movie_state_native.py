"""Playback state regression against owned MPV and generated media."""
import json
import subprocess
import threading
import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QTest

from tests.integration.test_movie_resume_mpv import media as media, qapp as qapp, sessions as sessions
from tests.integration.test_movie_safe_switch_mpv import routes as routes

pytestmark = pytest.mark.integration


@pytest.fixture
def clips(media, tmp_path):
    ffmpeg = media[0].parents[1] / "ffmpeg/ffmpeg.exe"
    path = tmp_path / "generated.mp4"
    subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=160x90:r=12:d=2", "-an", "-c:v",
                    "mpeg4", str(path)], check=True, timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
    second = tmp_path / "episode-two.mp4"
    second.write_bytes(path.read_bytes())
    return path, second


def until(predicate, limit=6):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return
        QTest.qWait(70)
    pytest.fail("owned playback deadline")


def item(first, second):
    return dict(vod_name="Generated episodes", vod_id="generated", first_url=str(first),
                play_url="One$" + str(first) + "#Two$" + str(second))


@pytest.mark.parametrize("case", ["bad_url", "missing_file"])
def test_failed_start_does_not_advance(sessions, tmp_path, clips, case):
    s = sessions(case)
    s.ctl.set_auto_next(True)
    original = "http://127.0.0.1:1/not-a-video" if case == "bad_url" else tmp_path / "absent.mp4"
    try:
        with patch.object(s.mpv, "loadfile", wraps=s.mpv.loadfile) as load:
            assert s.ctl.play(item(original, clips[1]))
            until(lambda: s.mpv.get_idle_active() is True)
            for _ in range(4):
                s.ctl.tick()
                QTest.qWait(100)
            assert load.call_count == 1
            assert not s.ctl.get_history()
            assert s.ctl.playback_phase() == "failed"
    finally:
        s.ctl.close()


@pytest.mark.parametrize("end", ["eof", "stop"])
def test_real_end_reason_gates_auto_next(sessions, clips, end):
    s = sessions(end)
    s.ctl.set_auto_next(True)
    try:
        assert s.ctl.play(item(*clips))
        if end == "stop":
            until(lambda: s.mpv.get_live_playback_time(.1) > .2)
            # Native MPV stop, without clearing the controller first.
            assert s.mpv.stop() is True

        def ticked():
            s.ctl.tick()
            return (s.ctl._current_index == 1 if end == "eof"
                    else s.ctl.current_item() is None)

        until(ticked)
        if end == "eof":
            until(lambda: s.mpv.get_property("path", timeout=.15) == str(clips[1]))
            until(lambda: (s.ctl.tick(), s.ctl.current_item() is None)[1])
            assert s.ctl.playback_phase() == "ended"
        else:
            assert s.ctl.playback_phase() == "stopped"
    finally:
        s.ctl.close()


def test_gui_tick_remains_responsive_with_real_ipc_lock_occupied(sessions, clips, qapp, tmp_path):
    from app.ui.theme.manager import get_theme_manager
    from app.ui.widgets.video_player_window import VideoPlayerWindow
    get_theme_manager(qapp)
    s = sessions("heartbeat")
    window = VideoPlayerWindow(s.ctl)
    locked = threading.Event()
    thread = None
    timer = QTimer()
    ticks = []
    try:
        assert s.ctl.play(item(*clips))
        def hold():
            with s.mpv._pipe_lock:
                locked.set()
                time.sleep(.65)
        thread = threading.Thread(target=hold)
        tick_timer = QTimer(window)
        tick_timer.setSingleShot(True)
        tick_timer.timeout.connect(window._tick)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(10)
        thread.start()
        assert locked.wait(.3)
        tick_timer.start(50)
        QTest.qWait(900)
        gap = max(b-a for a,b in zip(ticks,ticks[1:]))
        (tmp_path/"heartbeat.json").write_text(json.dumps({"max_gap_ms": round(gap*1000)}))
        assert gap < .22
    finally:
        timer.stop()
        if thread:
            thread.join(2)
        s.ctl.close()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_live_stalled_hls_stays_on_same_episode(sessions, routes, clips):
    urls, flags, _ = routes
    flags[0] = "stall"
    s = sessions("film-buffering")
    s.ctl.set_auto_next(True)
    s.mpv.extra_video_args += [
        "--cache=yes", "--demuxer-max-bytes=256KiB", "--demuxer-readahead-secs=2",
        "--cache-secs=2", "--cache-pause-initial=yes", "--cache-pause-wait=0.5",
    ]
    try:
        with patch.object(s.mpv, "loadfile", wraps=s.mpv.loadfile) as load:
            assert s.ctl.play(item(urls[0], clips[1]))
            until(lambda: (s.ctl.tick(), s.ctl.playback_phase() == "playing")[1], limit=10)
            until(lambda: (s.ctl.tick(), s.ctl.playback_phase() == "buffering")[1], limit=26)
            assert load.call_count == 1 and s.ctl._current_index == 0
            assert s.ctl._film_sample["paused-for-cache"] is True
            assert s.ctl.stop() is True
            assert s.ctl.playback_phase() == "stopped"
    finally:
        s.ctl.close()


def test_gui_heartbeat_when_owned_mpv_stops_answering(sessions, clips, qapp, tmp_path):
    import psutil
    from app.ui.theme.manager import get_theme_manager
    from app.ui.widgets.video_player_window import VideoPlayerWindow

    get_theme_manager(qapp)
    s = sessions("unresponsive")
    window = VideoPlayerWindow(s.ctl)
    timer = QTimer()
    ticks = []
    process = None
    try:
        assert s.ctl.play(item(*clips))
        process = psutil.Process(s.mpv._process.pid)
        process.suspend()
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(10)
        window._tick()
        QTest.qWait(450)
        assert window._native_poll is not None
        gap = max(b-a for a,b in zip(ticks,ticks[1:]))
        (tmp_path/"silent-heartbeat.json").write_text(json.dumps({"max_gap_ms": round(gap*1000)}))
        assert gap < .22
        assert s.ctl.playback_phase() == "loading"
    finally:
        if process is not None:
            process.resume()
        timer.stop()
        until(lambda: not window._native_poll._running)
        s.ctl.close()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_premature_eof_report_does_not_mark_complete_or_advance(sessions, clips):
    s = sessions("premature-eof")
    s.ctl.set_auto_next(True)
    try:
        with patch.object(s.mpv, "loadfile", wraps=s.mpv.loadfile) as load:
            assert s.ctl.play(item(*clips))
            until(lambda: (s.ctl.tick(), s.ctl.playback_phase() == "playing")[1])
            assert s.ctl._film_sample["time-pos"] < 1
            # Inject the ambiguous EOF reason after genuine measured progress.
            with patch.object(s.mpv, "film_end_reason", return_value="eof"):
                s.ctl.tick()
            assert load.call_count == 1 and s.ctl.playback_phase() == "failed"
    finally:
        s.ctl.close()
