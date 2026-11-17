"""Pre-admission legacy handoff with original business and owned real MPV."""

import os
from unittest.mock import patch

import pytest

from app.core.operation_result import OperationStatus as S
from tests.integration.test_goal_admission_mpv import run_worker
from tests.integration.test_goal_adapter_mpv import charts
from tests.integration.test_music_shutdown_restart_mpv import progressing
from tests.unit.test_listen_music_trial import attach, joined, speak
from tools.goal_execute_b3.lab import Lab

pytestmark = pytest.mark.integration


def test_successful_legacy_cold_start_then_same_goal_is_no_op(tmp_path, monkeypatch):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            source = charts(lab)
            rt, trial, _, _, values, workers = attach(c, lab.root, monkeypatch)
            trial.set_enabled(True)
            with (patch.object(c, "recommend_music", wraps=c.recommend_music) as legacy,
                  patch.object(c, "admit_music_goal", wraps=c.admit_music_goal) as admit,
                  patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc):
                speak(rt, "我想听歌了")
                joined(workers)
                assert values[0][1] is None and values[0][0].status is S.CONFIRMED
                assert legacy.call_count == 1 and not admit.called
                progressing(c, c.mpv.get_property("path"))
                before, calls = c.queue.snapshot(c.get_current_song()), len(source.calls)
                speak(rt, "我想听歌了")
                joined(workers)
                assert values[1][0].outcome.decision.plan == "NO_OP"
                assert values[1][0].status is S.CONFIRMED
                assert admit.call_count == 1 and legacy.call_count == 1
                assert len(source.calls) == calls and c.queue.snapshot(c.get_current_song()) == before
                assert len([x for x in rpc.call_args_list if x.args[0] == "loadfile"]) == 1
            rt.stop()
            trial.adapter.close()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)


@pytest.mark.parametrize("action", ["manual", "close"])
def test_inflight_legacy_read_releases_locks_and_cannot_overwrite_or_restart(tmp_path, monkeypatch, action):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            song, _ = lab.add_collection(lab.generate("manual"))
            source = charts(lab)
            source.release.clear()
            rt, trial, _, _, values, workers = attach(c, lab.root, monkeypatch)
            trial.set_enabled(True)
            with patch.object(c, "admit_music_goal", side_effect=AssertionError("no B4 route")):
                try:
                    speak(rt, "我想听歌了")
                    assert source.entered.wait(2)
                    # No controller or authority locks are retained across source IO.
                    for lock in (c._lock, c.db._lock, c._goal_admission._lock):
                        assert lock.acquire(timeout=0.1)
                        lock.release()
                    if action == "manual":
                        c.play_song(song)
                        progressing(c, song["path"])
                    else:
                        rt.stop()
                        c.stop()
                    source.release.set()
                    joined(workers)
                    assert values[0][1] is None and values[0][0].status is not S.CONFIRMED
                    if action == "manual":
                        assert c.mpv.get_property("path") == song["path"]
                        assert c.get_current_song()["id"] == song["id"]
                    else:
                        assert not c.mpv.is_running()
                finally:
                    source.release.set()
                    joined(workers)
                    rt.stop()
                    trial.adapter.close()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)
