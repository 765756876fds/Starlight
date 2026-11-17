"""Original Lab teardown, real owned MPV; deterministic late poll and live heal."""

import os
import threading
import time
from unittest.mock import patch

from tools.goal_execute_b3.lab import Lab
from tests.integration.test_goal_admission_mpv import run_worker


def progressing(c, path, seconds=2):
    end = time.monotonic() + seconds
    previous = None
    while time.monotonic() < end:
        actual = c.mpv.get_property("path", timeout=0.1)
        position = c.mpv.get_property("time-pos", timeout=0.1)
        if actual == path and type(position) in (float, int):
            if previous is not None and position > previous:
                return
            previous = position
        time.sleep(0.02)
    raise AssertionError("matching owned MPV must advance")


def test_late_real_idle_poll_after_stop_does_not_restart(tmp_path):
    def body():
        # Deliberately use the original Lab, not B5 OrderedLab.
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            entered, release, exited = threading.Event(), threading.Event(), threading.Event()
            running = c.mpv.is_running

            def stopped_poll():
                if threading.current_thread().name == "music-idle-poll":
                    entered.set()
                    assert release.wait(2)
                    exited.set()
                    return False
                return running()

            with (
                patch.object(c.mpv, "is_running", side_effect=stopped_poll),
                patch.object(c.mpv, "start", wraps=c.mpv.start) as start,
            ):
                try:
                    assert entered.wait(2)
                    c.stop()
                finally:
                    release.set()
                assert exited.wait(1)
                polls = [
                    t
                    for t in set(threading.enumerate()) - lab.before_threads
                    if t.name == "music-idle-poll"
                ]
                for thread in polls:
                    thread.join(2)
                    assert not thread.is_alive()
                start.assert_not_called()
            assert not c.mpv.is_running() and not c._mpv_restart_pending
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)


def test_live_owned_mpv_exit_self_heals_and_preserves_progress_contract(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            song, _ = lab.add_collection(lab.generate())
            c.play_song(song)
            progressing(c, song["path"])
            confirms = [
                t
                for t in set(threading.enumerate()) - lab.before_threads
                if t.name == "music-playback-confirm"
            ]
            for thread in confirms:
                thread.join(2)
                assert not thread.is_alive(), "establish stable playback before crash injection"
            original = c.mpv._process
            original.terminate()  # Only this lab's process, no taskkill/name lookup.
            original.wait(2)
            end = time.monotonic() + 5
            while time.monotonic() < end:
                process = c.mpv._process
                if (
                    process is not None
                    and process.pid != original.pid
                    and not c._mpv_restart_pending
                    and process.poll() is None
                ):
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("live crash must self-heal within bound")
            assert c._mpv_restart_count == 1
            progressing(c, song["path"])
            c.stop()
            assert not c.mpv.is_running()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)
