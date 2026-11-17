"""One pre-admission route; temporary authority DB, fake IPC, no network."""

from dataclasses import replace
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from app.core.operation_result import OperationResult, OperationStatus as S
from app.music.goal_admission import AdmissionStatus, GoalReceipt
from app.music.goal_adapter import AdapterResult
from tests.unit.test_goal_admission import setup as setup  # noqa: F401
from tests.unit.test_goal_adapter import chart_source
from tests.unit.test_listen_music_trial import attach, joined, speak, prefer


def trial_for(c, monkeypatch):
    chart_source(c)
    result = attach(c, Path(c.db_path).parent, monkeypatch)
    result[1].set_enabled(True)
    return result


@pytest.mark.parametrize("problem", ["read_error", "busy", "unknown_member", "partial"])
def test_uncertain_inventory_never_becomes_empty_collection(setup, monkeypatch, problem):
    c, p, _, _, _ = setup
    rt, trial, _, _, values, workers = trial_for(c, monkeypatch)
    capture = trial.adapter.resources.capture
    entered, release = threading.Event(), threading.Event()

    def hold_database():
        with c.db._lock:
            entered.set()
            assert release.wait(2)

    reader = None
    if problem == "busy":
        reader = threading.Thread(target=hold_database)
        reader.start()
        assert entered.wait(1)
    elif problem == "read_error":
        monkeypatch.setattr(trial.adapter.resources, "capture", lambda *a: (_ for _ in ()).throw(OSError("fixture")))
    elif problem == "unknown_member":
        monkeypatch.setattr(c, "goal_member", lambda *a: (_ for _ in ()).throw(PermissionError("fixture")))
    else:
        monkeypatch.setattr(trial.adapter.resources, "capture", lambda *a: replace(capture(*a), collection_complete=False))
    try:
        with (patch.object(c, "recommend_music", side_effect=AssertionError("not empty")),
              patch.object(c, "admit_music_goal", side_effect=AssertionError("not attemptable"))):
            speak(rt, "我想听歌了")
            joined(workers)
        assert values[0][1] is None and values[0][0].status is not S.CONFIRMED
        assert not p.loads
    finally:
        release.set()
        if reader:
            reader.join(1)
            assert not reader.is_alive()
        rt.stop()


@pytest.mark.parametrize("change", ["manual", "member", "source", "voice", "disabled", "expired"])
def test_change_after_route_decision_does_not_invoke_legacy(setup, monkeypatch, change):
    c, p, song, pid, mid = setup
    rt, trial, _, _, values, workers = trial_for(c, monkeypatch)
    original = trial.adapter.validate_prepared

    def mutate(ticket, prepared):
        if change == "manual":
            c.play_song(song)
        elif change == "member":
            c.db.remove_from_playlist(pid, mid)
        elif change == "source":
            chart_source(c)
        elif change == "voice":
            trial.voice_advanced()
        elif change == "disabled":
            trial.set_enabled(False)
        else:
            prepared = replace(prepared, state=replace(prepared.state, captured=prepared.state.captured - 2))
        return original(ticket, prepared)

    with (patch.object(trial.adapter, "validate_prepared", side_effect=mutate),
          patch.object(c, "recommend_music", side_effect=AssertionError("stale route")),
          patch.object(c, "admit_music_goal", side_effect=AssertionError("no B4 route"))):
        speak(rt, "我想听歌了")
        joined(workers)
    assert values[0][1] is None and values[0][0].status is not S.CONFIRMED
    assert len(p.loads) == (1 if change == "manual" else 0)
    rt.stop()


def test_owner_rechecks_database_changes_in_handoff_gap(setup, monkeypatch):
    c, p, _, pid, mid = setup
    rt, trial, _, _, values, workers = trial_for(c, monkeypatch)
    transfer = c.recommend_music_guarded_route

    def mutate(*args):
        c.db.remove_from_playlist(pid, mid)
        return transfer(*args)

    with (patch.object(c, "recommend_music_guarded_route", side_effect=mutate),
          patch.object(c, "recommend_music", side_effect=AssertionError("owner must reject"))):
        speak(rt, "我想听歌了")
        joined(workers)
    assert values[0][0].status is S.UNAVAILABLE and values[0][1] is None
    assert not p.loads
    rt.stop()


@pytest.mark.parametrize("status", list(AdmissionStatus))
def test_no_b4_result_can_trigger_legacy(setup, monkeypatch, status):
    c, _, _, _, _ = setup
    prefer(c)
    rt, trial, _, _, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    result = AdapterResult(status.value, "fixture", receipt=GoalReceipt("fixture", status, "fixture"))
    with (patch.object(trial.adapter, "run", return_value=result) as execute,
          patch.object(c, "recommend_music", side_effect=AssertionError("no post-B4 fallback"))):
        speak(rt, "我想听歌了")
        joined(workers)
    assert execute.call_count == 1 and values[0][1] is None
    rt.stop()


def test_same_permit_is_not_reissued_with_a_new_operation_id(setup, monkeypatch):
    c, p, _, _, _ = setup
    prefer(c)
    rt, trial, _, commands, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    speak(rt, "我想听歌了")
    joined(workers)
    assert values[0][0].status is S.CONFIRMED
    operation = trial._ticket.operation_id
    duplicate = c.command_bus.execute(commands[0])
    assert duplicate.status is S.UNKNOWN and "已经处理" in duplicate.message
    assert trial._ticket.operation_id == operation and len(p.loads) == 1
    rt.stop()


@pytest.mark.parametrize("status", [S.ACCEPTED, S.UNKNOWN, S.FAILED])
def test_ambiguous_legacy_attempt_stops_later_trial_without_new_load(setup, monkeypatch, status):
    c, p, _, _, _ = setup
    rt, trial, _, _, values, workers = trial_for(c, monkeypatch)
    with patch.object(c, "recommend_music", return_value=OperationResult(status, "fixture")) as legacy:
        speak(rt, "我想听歌了")
        joined(workers)
        speak(rt, "我想听歌了")
        joined(workers)
    assert legacy.call_count == 1 and not p.loads
    assert values[0][0].status is status and values[1][0].status is S.UNKNOWN
    assert "仍未确认" in values[1][0].message
    rt.stop()


def test_foreign_action_cancels_already_claimed_legacy_guard(setup, monkeypatch):
    c, p, song, _, _ = setup
    rt, trial, _, _, values, workers = trial_for(c, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = c.recommend_music

    def hold(**kwargs):
        entered.set()
        assert release.wait(2)
        assert not kwargs["is_current"]()
        return original(**kwargs)

    with patch.object(c, "recommend_music", side_effect=hold):
        try:
            speak(rt, "我想听歌了")
            assert entered.wait(1)
            c.play_song(song)
            release.set()
            joined(workers)
        finally:
            release.set()
            joined(workers)
    assert len(p.loads) == 1 and values[0][0].status is not S.CONFIRMED
    assert c.get_current_song()["id"] == song["id"]
    rt.stop()


@pytest.mark.parametrize("route", ["legacy_unknown", "b4_unknown"])
def test_disabling_does_not_erase_unresolved_effects_or_issue_new_operation(setup, monkeypatch, route):
    c, p, _, _, _ = setup
    if route == "b4_unknown":
        prefer(c)
        p.progress = False
        rt, trial, _, _, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
        trial.set_enabled(True)
    else:
        rt, trial, _, _, values, workers = trial_for(c, monkeypatch)
    original = c.recommend_music
    with patch.object(c, "recommend_music", return_value=OperationResult(S.UNKNOWN, "fixture")) as legacy:
        if route == "b4_unknown":
            legacy.side_effect = original
        speak(rt, "我想听歌了")
        joined(workers)
        old_operation = trial._ticket.operation_id
        calls, loads = legacy.call_count, len(p.loads)
        assert trial.adapter.unresolved or trial._legacy_unresolved
        trial.set_enabled(False)
        speak(rt, "我想听歌了")
        joined(workers)
    assert values[-1][0].status is S.UNKNOWN
    assert legacy.call_count == calls and len(p.loads) == loads
    assert trial._ticket.operation_id == old_operation
    rt.stop()
