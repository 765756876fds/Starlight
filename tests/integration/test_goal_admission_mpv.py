"""Opt-in formal controller admission with generated files and real owned MPV."""

from dataclasses import replace, asdict
import json
import os
import threading
import time
from unittest.mock import patch

import pytest

from app.music.goal_admission import GoalRequest, AdmissionStatus as S
from tools.goal_execute_b3.lab import Lab

pytestmark = pytest.mark.integration


def run_worker(function, budget=12):
    results, failures = [], []

    def run():
        try:
            results.append(function())
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run, name="b4-native-worker")
    worker.start()
    worker.join(budget)
    assert not worker.is_alive(), "bounded native admission worker timeout"
    if failures:
        raise failures[0]
    return results[0] if results else None


def make_request(lab, plan="TRY_COLLECTION", name="native", budget=3):
    c = lab.ctrl
    c.state_store.set("movie", "playing", False)
    song, pid = lab.add_collection(lab.generate(name))
    mid = c.db.get_playlist_songs(pid)[-1]["id"]
    member = c.goal_member(pid, mid)
    token = c.goal_state()
    return GoalRequest(
        name, plan, token, time.monotonic() + budget, member if plan == "TRY_COLLECTION" else None
    ), song


def confirmed(c, request, budget=2):
    deadline = time.monotonic() + budget
    value = None
    while time.monotonic() < deadline:
        value = c.observe_music_goal(request.operation_id)
        if value.status in (S.STARTED, S.REJECTED_STALE, S.UNKNOWN_EFFECTS):
            return value
        time.sleep(0.02)
    return value


@pytest.mark.parametrize("plan", ["TRY_COLLECTION", "TRY_CHART", "NO_OP", "RESUME_MUSIC"])
def test_real_owned_admission_and_started_with_original_business(tmp_path, plan):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, song = make_request(lab)
            if plan == "TRY_CHART":
                lab.charts([lab.generate("chart-a"), lab.generate("chart-b")])

                def forbidden_movie_control():
                    pytest.fail("B4 must not invoke cross-media pause")

                c.set_movie_pauser(forbidden_movie_control)
            if plan in ("NO_OP", "RESUME_MUSIC"):
                c.play_song(song)  # real legacy path, no experimental action_gate
                if plan == "RESUME_MUSIC":
                    c.pause()
                time.sleep(0.06)
            req = replace(
                req,
                plan=plan,
                expected=c.goal_state(),
                member=req.member if plan == "TRY_COLLECTION" else None,
            )
            before = c.queue.snapshot(c.get_current_song())
            with patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc:
                result = c.admit_music_goal(req)
                assert result.status is S.ACCEPTED
                assert not result.safe_to_fallback
                duplicate = c.admit_music_goal(req)
                assert duplicate == result
                final = confirmed(c, req)
                assert final.status is S.STARTED, final
                assert final.proof.position > final.proof.previous_position
                (tmp_path / "b4-native-receipt.json").write_text(
                    json.dumps(asdict(final), indent=2), encoding="utf8"
                )
                loads = [x for x in rpc.call_args_list if x.args[0] == "loadfile"]
                assert len(loads) == (0 if plan in ("NO_OP", "RESUME_MUSIC") else 1)
            assert lab.manual_revision == 0  # no B3 experimental serialization
            if plan in ("NO_OP", "RESUME_MUSIC"):
                assert c.queue.snapshot(c.get_current_song()) == before
            if plan == "TRY_CHART":
                assert c.queue.size() == 2 and c.recommendations.session
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)


def test_real_accept_then_user_switch_makes_old_confirmation_stale(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, _ = make_request(lab)
            assert c.admit_music_goal(req).status is S.ACCEPTED
            other, _ = lab.add_collection(lab.generate("later-manual"))
            c.play_song(other)
            assert c.observe_music_goal(req.operation_id).status is S.REJECTED_STALE
            assert c.get_current_song()["id"] == other["id"]
            assert c.mpv.get_property("path") == other["path"]

    run_worker(body)


def test_real_slow_source_late_result_cannot_replace_manual_song(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, song = make_request(lab)
            source = lab.charts([lab.generate("chart-only")])
            source.release.clear()
            req = replace(req, plan="TRY_CHART", member=None, expected=c.goal_state())
            values = []
            task = threading.Thread(target=lambda: values.append(c.admit_music_goal(req)))
            task.start()
            try:
                assert source.entered.wait(1)
                c.play_song(song)
                source.release.set()
                task.join(2)
                assert not task.is_alive()
                assert values[0].status is S.REJECTED_STALE
                assert c.get_current_song()["id"] == song["id"]
                assert c.mpv.get_property("path") == song["path"]
            finally:
                source.release.set()
                task.join(2)

    run_worker(body)


def test_real_corrupt_media_and_lost_ack_never_create_false_started(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            song, pid = lab.add_collection(lab.generate("corrupt", broken=True))
            c.state_store.set("movie", "playing", False)
            mid = c.db.get_playlist_songs(pid)[0]["id"]
            req = GoalRequest(
                "corrupt",
                "TRY_COLLECTION",
                c.goal_state(),
                time.monotonic() + 0.6,
                c.goal_member(pid, mid),
            )
            value = c.admit_music_goal(req)
            assert value.status is S.ACCEPTED and value.mpv_accepted
            final = confirmed(c, req, 1)
            assert (
                final.status in (S.UNKNOWN_EFFECTS, S.REJECTED_STALE) and not final.safe_to_fallback
            )
            assert c.mpv.get_property("idle-active") is True

    run_worker(body)


def test_real_rejected_member_does_not_change_queue_history_or_player(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, _ = make_request(lab)
            token = req.member
            assert c.db.remove_from_playlist(token.playlist_id, token.member_id)
            before = (c.queue.snapshot(), c.db._conn.total_changes, c.get_current_song())
            with patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc:
                result = c.admit_music_goal(req)
                assert result.status is S.REJECTED_STALE and not result.effects_started
                assert not [x for x in rpc.call_args_list if x.args[0] == "loadfile"]
            assert before == (c.queue.snapshot(), c.db._conn.total_changes, c.get_current_song())

    run_worker(body)


def test_real_manual_selection_before_admission_wins_without_old_load(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, _ = make_request(lab)
            song, _ = lab.add_collection(lab.generate("manual-new"))
            c.play_song(song)
            before = c.queue.snapshot(c.get_current_song())
            with patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc:
                value = c.admit_music_goal(req)
                assert value.status is S.REJECTED_STALE
                assert not [x for x in rpc.call_args_list if x.args[0] == "loadfile"]
            assert before == c.queue.snapshot(c.get_current_song())
            assert c.mpv.get_property("path") == song["path"]

    run_worker(body)


def test_real_safe_pre_effect_failure_can_fall_back_to_original_chart_service(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, _ = make_request(lab)
            lab.charts([lab.generate("fallback-chart")])
            req = replace(req, expected=c.goal_state())
            before = c.queue.snapshot(c.get_current_song())
            with patch(
                "app.music.goal_admission.file_identity",
                side_effect=PermissionError("generated read refusal"),
            ):
                result = c.admit_music_goal(req)
            assert result.status is S.FAILED_NO_EFFECT and result.safe_to_fallback
            assert before == c.queue.snapshot(c.get_current_song())
            fallback = replace(req, operation_id="chart-fallback", plan="TRY_CHART", member=None)
            assert c.admit_music_goal(fallback).status is S.ACCEPTED
            assert confirmed(c, fallback).status is S.STARTED

    run_worker(body)


def test_real_unknown_before_send_blocks_auto_and_second_plan(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, _ = make_request(lab)
            with (
                patch.object(c.mpv, "loadfile_guarded", return_value=None),
                patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc,
            ):
                value = c.admit_music_goal(req)
                assert (
                    value.status is S.UNKNOWN_EFFECTS
                    and value.effects_started
                    and not value.safe_to_fallback
                )
                c._auto_next()
                assert not [x for x in rpc.call_args_list if x.args[0] == "loadfile"]
            assert (
                c.admit_music_goal(replace(req, operation_id="other")).reason
                == "previous_operation_unresolved"
            )

    run_worker(body)


def test_real_shutdown_cancels_pending_without_late_load_or_worker_leak(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, _ = make_request(lab)
            source = lab.charts([lab.generate("pending-chart")])
            source.release.clear()
            req = replace(req, expected=c.goal_state(), plan="TRY_CHART", member=None)
            values = []
            worker = threading.Thread(target=lambda: values.append(c.admit_music_goal(req)))
            with patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc:
                worker.start()
                assert source.entered.wait(1)
                c.stop()
                worker.join(2)
                assert not worker.is_alive() and values[0].status is S.REJECTED_STALE
                assert not [x for x in rpc.call_args_list if x.args[0] == "loadfile"]

    run_worker(body)


def test_real_chart_cancel_during_ack_cannot_publish_old_state_or_build_queue(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, _ = make_request(lab)
            lab.charts([lab.generate("ack-chart")])
            req = replace(req, expected=c.goal_state(), plan="TRY_CHART", member=None)
            load = c.mpv.loadfile_guarded
            before = (c.get_current_song(), c.queue.snapshot(), c.db._conn.total_changes)

            def cancel_after_ack(*args):
                result = load(*args)
                assert result.get("error") == "success"
                c.cancel_music_goal(req.operation_id)
                return result

            with patch.object(c.mpv, "loadfile_guarded", side_effect=cancel_after_ack):
                result = c.admit_music_goal(req)
            assert result.status is S.REJECTED_STALE and result.effects_started
            assert not result.safe_to_fallback
            assert before == (c.get_current_song(), c.queue.snapshot(), c.db._conn.total_changes)
            assert c.observe_music_goal(req.operation_id).status is S.REJECTED_STALE

    run_worker(body)


def test_real_chart_unconfirmed_load_stops_internal_retry_and_outer_fallback(tmp_path):
    def body():
        with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            req, _ = make_request(lab)
            lab.charts([lab.generate("first"), lab.generate("second")])
            req = replace(req, expected=c.goal_state(), plan="TRY_CHART", member=None)
            with patch.object(c.mpv, "loadfile_guarded", return_value=None) as load:
                result = c.admit_music_goal(req)
            assert result.status is S.UNKNOWN_EFFECTS and not result.safe_to_fallback
            assert load.call_count == 1
            assert c.queue.size() == 0 and c.get_current_song() is None
            assert (
                c.admit_music_goal(replace(req, operation_id="fallback")).reason
                == "previous_operation_unresolved"
            )

    run_worker(body)
