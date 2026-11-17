"""Parsed voice text -> existing bus -> B4 -> owned MPV; no microphone/network."""

from dataclasses import asdict
import json
import os
from unittest.mock import patch

import pytest

from app.core.operation_result import OperationStatus as S
from tests.integration.test_goal_adapter_mpv import charts
from tests.integration.test_goal_admission_mpv import run_worker
from tests.integration.test_music_shutdown_restart_mpv import progressing
from tests.unit.test_listen_music_trial import attach, joined, speak, prefer
from tools.goal_execute_b3.lab import Lab

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("scenario,plan", [("playing", "NO_OP"), ("paused", "RESUME_MUSIC"),
                                         ("collection", "TRY_COLLECTION")])
def test_voice_text_owned_admission_and_progress(tmp_path, monkeypatch, scenario, plan):
    def body():
        with Lab(tmp_path / "中文隔离", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            song, _ = lab.add_collection(lab.generate("collection"))
            if scenario == "collection":
                prefer(c)
            if scenario in ("playing", "paused"):
                c.play_song(song)
                progressing(c, song["path"])
                if scenario == "paused":
                    c.pause()
            rt, trial, center, commands, values, workers = attach(c, lab.root, monkeypatch)
            trial.set_enabled(True)
            before = c.queue.snapshot(c.get_current_song())
            with (patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc,
                  patch.object(c, "admit_music_goal", wraps=c.admit_music_goal) as admit,
                  patch.object(c, "recommend_music", side_effect=AssertionError("no legacy retry"))):
                speak(rt, "我想听歌了。")
                joined(workers)
            result, error = values[0]
            assert error is None and result.status is S.CONFIRMED, result
            outcome = result.outcome
            assert outcome.decision.plan == plan and outcome.status == "started"
            assert outcome.receipt.proof.position > outcome.receipt.proof.previous_position
            assert admit.call_count == 1
            assert admit.call_args.args[0].operation_id == outcome.receipt.operation_id
            loads = [call for call in rpc.call_args_list if call.args[0] == "loadfile"]
            assert len(loads) == (1 if scenario == "collection" else 0)
            if scenario != "collection":
                assert c.queue.snapshot(c.get_current_song()) == before
            record, = center.history()
            assert record.id == commands[0].notification_id and record.status == "success"
            rt._enqueue_speak.assert_not_called()
            progressing(c, song["path"])
            (tmp_path / "voice-goal-proof.json").write_text(json.dumps(asdict(outcome), indent=2))
            rt.stop()
            trial.adapter.close()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)


def test_flag_off_and_later_disabled_use_real_legacy_chart_business(tmp_path, monkeypatch):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            source = charts(lab)
            rt, trial, _, commands, values, workers = attach(c, lab.root, monkeypatch)
            with patch.object(trial.adapter, "run", side_effect=AssertionError("disabled")):
                speak(rt, "我想听歌了")
                joined(workers)
                assert values[0][1] is None and values[0][0].status is S.CONFIRMED
                assert commands[0].goal_trial is None
                progressing(c, c.mpv.get_property("path"))
                assert c.queue.size() == 2 and c.recommendations.session
                trial.set_enabled(True)
                trial.set_enabled(False)
                speak(rt, "我想听歌了")
                joined(workers)
                assert len(values) == 2 and values[-1][0].status is S.CONFIRMED
                assert commands[-1].goal_trial is None and len(source.calls) >= 3
            rt.stop()
            trial.adapter.close()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)


@pytest.mark.parametrize("movie", [True, None])
def test_movie_boundary_never_loads_or_pauses_either_player(tmp_path, monkeypatch, movie):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            lab.add_collection(lab.generate())
            c.state_store.set("movie", "playing", movie)
            rt, trial, _, _, values, workers = attach(c, lab.root, monkeypatch)
            trial.set_enabled(True)
            with patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc:
                speak(rt, "我想听歌了")
                joined(workers)
            assert values[0][1] is None and values[0][0].status is S.NEEDS_PARAMETER
            assert lab.movie_calls == 0
            assert not [call for call in rpc.call_args_list if call.args[0] in ("loadfile", "set_property")]
            rt.stop()
            trial.adapter.close()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)


def test_corrupt_media_is_not_success_and_does_not_retry(tmp_path, monkeypatch):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            lab.add_collection(lab.generate(broken=True))
            prefer(c)
            rt, trial, center, _, values, workers = attach(c, lab.root, monkeypatch)
            trial.set_enabled(True)
            with (patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc,
                  patch.object(c, "recommend_music", side_effect=AssertionError("no fallback"))):
                speak(rt, "我想听歌了")
                joined(workers)
            assert values[0][1] is None and values[0][0].status is not S.CONFIRMED
            assert center.history()[0].status != "success"
            assert len([call for call in rpc.call_args_list if call.args[0] == "loadfile"]) == 1
            rt.stop()
            trial.adapter.close()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)
