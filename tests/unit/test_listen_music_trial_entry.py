"""Application entry/metadata/path boundary; only owned temporary resources."""

import logging
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core.operation_result import OperationResult, OperationStatus as S
from app.music.goal_adapter import AdapterResult, GoalAdapter, Prepared
from app.music.goal_admission import AdmissionStatus, GoalReceipt, SnapshotError
from app.music.goal_trial_resources import TrialResources
from app.voice.listen_music_trial import ListenMusicTrial
from app.voice.runtime import VoiceRuntime
from tests.unit.test_goal_admission import setup as setup  # noqa: F401
from tests.unit.test_goal_adapter import chart_source
from tests.unit.test_goal_shadow_b2 import in_worker
from tests.unit.test_listen_music_trial import attach, joined, prefer, speak


def production(trial, c):
    trial.adapter.close()
    trial.adapter = GoalAdapter.for_user_trial(c)
    return trial


def assemble(c, runtime):
    """Replace only this fixture's unused initial binding, then use real main assembly."""
    from main import Application

    previous = runtime.listen_music_trial
    if previous is not None:
        previous.adapter.close()
    runtime.listen_music_trial = None
    c._listen_music_trial_ref = None
    app = Application.__new__(Application)
    app.music, app.voice, app.listen_music_trial = c, runtime, None
    app._movie_panel = None
    app._assemble_listen_music_trial()
    assert app.listen_music_trial is not None
    return app, app.listen_music_trial


def test_main_assembly_is_idempotent_same_bus_and_no_io(setup):
    from main import Application

    c, player, _, _, _ = setup
    runtime = VoiceRuntime({})
    runtime.command_bus = c.command_bus
    app = Application.__new__(Application)
    app.music, app.voice, app.listen_music_trial = c, runtime, None
    with (patch.object(Path, "resolve", side_effect=AssertionError("assembly IO")),
          patch.object(c, "goal_state", side_effect=AssertionError("assembly IPC")),
          patch.object(c.recommendations, "choices", side_effect=AssertionError("assembly network")),
          patch.object(c.music_memory, "profile", side_effect=AssertionError("preference initialization"))):
        app._assemble_listen_music_trial()
        original = app.listen_music_trial
        app._assemble_listen_music_trial()
    assert original is app.listen_music_trial is runtime.listen_music_trial
    assert c._listen_music_trial_ref() is original
    assert not original.snapshot().enabled and not original.snapshot().collection_authorized
    assert not player.loads and not c.music_memory.path.exists()
    original.adapter.close()
    runtime.stop()


@pytest.mark.parametrize("condition,expected", [("fresh", False), ("used", None),
    ("running", None), ("unknown_process", None), ("external", None), ("busy", None)])
def test_only_proven_never_started_movie_publishes_initial_idle(setup, condition, expected):
    from main import Application

    c, _, _, _, _ = setup
    c.state_store.set("movie", "playing", None)
    app = Application.__new__(Application)
    app.music = c
    movie = SimpleNamespace(_media_action_lock=threading.Lock(), _playback_gen=0,
                            _current=None, _external_url_mode=False, _stopping=False,
                            mpv=SimpleNamespace(is_running=lambda: False))
    movie._write_state = lambda **kw: c.state_store.set("movie", "playing", kw["playing"])
    app._movie_panel = SimpleNamespace(_movie=movie)
    if condition == "used":
        movie._playback_gen = 1
    if condition in ("running", "unknown_process"):
        movie.mpv.is_running = lambda: True if condition == "running" else None
    if condition == "external":
        movie._external_url_mode = True
    if condition == "busy":
        movie._media_action_lock.acquire()
    try:
        in_worker(app._prepare_goal_trial_context)
        assert c.state_store.get("movie", "playing", None) is expected
    finally:
        if condition == "busy":
            movie._media_action_lock.release()


def test_production_capture_never_reads_private_members_or_preferences(setup):
    c, player, _, _, _ = setup
    source = chart_source(c)
    prefer(c)
    adapter = GoalAdapter.for_user_trial(c)
    with (patch.object(adapter.resources, "_query", side_effect=AssertionError("private SQL")),
          patch.object(c, "goal_member", side_effect=AssertionError("private member")),
          patch.object(c.recommendations, "choices", side_effect=AssertionError("source preparation"))):
        prepared = in_worker(lambda: adapter.prepare(adapter.begin()))
    assert isinstance(prepared, Prepared) and prepared.decision.plan == "TRY_CHART"
    assert all(item.plan != "TRY_COLLECTION" for item in prepared.resources.candidates)
    assert not player.loads and not source.calls


@pytest.mark.parametrize("kind", ["outside", "whole_disk", "different_parent", "hardlink"])
def test_managed_paths_fail_closed(setup, tmp_path, kind):
    c, _, _, _, _ = setup
    reader = TrialResources(c)
    path = c.db_path
    if kind == "outside":
        path = tmp_path / "unapproved.db"
    elif kind == "whole_disk":
        reader.root = Path(Path(c.db_path).anchor)
    elif kind == "different_parent":
        reader._paths = (reader._paths[0], tmp_path / "another" / "preferences.db")
    else:
        (tmp_path / "hardlinked.db").hardlink_to(c.db_path)
    with pytest.raises(SnapshotError):
        in_worker(lambda: reader.inside(path))


def test_real_windows_junction_is_rejected(setup, tmp_path):
    c, _, _, _, _ = setup
    link = tmp_path / "junction"
    target = tmp_path / "target"
    target.mkdir()
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, timeout=3)
    assert result.returncode == 0, result.stderr
    reader = TrialResources(c)
    reader._paths = (link / "music.db", link / "music_preferences.db")
    reader.root = link
    with pytest.raises(SnapshotError, match="reparse"):
        in_worker(lambda: reader.inside(reader._paths[0]))


def test_managed_path_requires_worker_and_does_not_create_missing_preferences(setup):
    c, _, _, _, _ = setup
    reader = TrialResources(c)
    with pytest.raises(SnapshotError):
        reader.inside(c.db_path)
    assert in_worker(lambda: reader.inside(c.music_memory.path)) == c.music_memory.path
    assert not c.music_memory.path.exists()


def test_forged_local_preparation_cannot_bypass_production_collection_limit(setup):
    c, _, _, _, _ = setup
    isolated = GoalAdapter(c, isolation_root=Path(c.db_path).parent, enabled=True)
    ticket = isolated.begin()
    prepared = in_worker(lambda: isolated.prepare(ticket))
    assert prepared.decision.plan == "TRY_COLLECTION"
    isolated.resources = TrialResources(c)
    with patch.object(c, "admit_music_goal", side_effect=AssertionError("not authorized")):
        result = in_worker(lambda: isolated.execute(ticket, prepared))
    assert result.status == "rejected_unavailable" and not isolated.unresolved


def test_route_traces_do_not_change_disabled_legacy_result_or_include_private_text(setup, monkeypatch, caplog):
    c, _, _, _, _ = setup
    rt, trial, _, commands, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    production(trial, c)
    result = OperationResult(S.ACCEPTED, "private-title https://private.invalid/token")
    old = Mock(return_value=result)
    monkeypatch.setattr(c, "recommend_music", old)
    with caplog.at_level(logging.INFO, logger="app.voice.listen_music_trial"):
        speak(rt, "我想听歌了")
        joined(workers)
    assert values == [(result, None)] and commands[0].goal_trial is None
    assert trial.snapshot().route == "V1" and not trial.snapshot().enabled
    lines = "\n".join(record.message for record in caplog.records if "ListenMusicTrial" in record.message)
    assert "route=V1" in lines and "receipt=accepted" in lines
    assert "private" not in lines and "我想听歌了" not in lines
    assert trial.snapshot().operation_id == commands[0].notification_id
    rt.stop()


def test_pre_admission_route_is_visible_and_once_only(setup, monkeypatch):
    c, _, _, _, _ = setup
    chart_source(c)
    rt, trial, _, _, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    production(trial, c)
    trial.set_enabled(True)
    old = Mock(return_value=OperationResult(S.CONFIRMED, "fixture legacy proof"))
    monkeypatch.setattr(c, "recommend_music", old)
    with patch.object(c, "admit_music_goal", side_effect=AssertionError("pre admission only")):
        speak(rt, "我想听歌了")
        joined(workers)
    state = trial.snapshot()
    assert values[0][0].status is S.CONFIRMED and old.call_count == 1
    assert state.route == "V1_PRE_ADMISSION" and state.plan == "TRY_CHART"
    assert not state.protected and state.receipt == "confirmed"
    rt.stop()


@pytest.mark.parametrize("status", [AdmissionStatus.ACCEPTED, AdmissionStatus.UNKNOWN_EFFECTS])
def test_pending_survives_ui_disable_and_does_not_fall_back(setup, monkeypatch, status):
    c, player, song, _, _ = setup
    c.play_song(song)
    rt, trial, _, _, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    production(trial, c)
    trial.set_enabled(True)
    def admission(request):
        return GoalReceipt(request.operation_id, status, "fixture pending", True)
    with (patch.object(c, "admit_music_goal", side_effect=admission) as admit,
          patch.object(trial.adapter, "observe", return_value=AdapterResult("unknown_effects", "fixture")),
          patch.object(c, "recommend_music", side_effect=AssertionError("no legacy after admission"))):
        speak(rt, "我想听歌了")
        joined(workers)
        assert values[0][0].status is not S.CONFIRMED
        trial.set_enabled(False)
        assert not trial.snapshot().enabled and trial.snapshot().protected
        speak(rt, "我想听歌了")
        joined(workers)
        assert values[1][0].status is S.UNKNOWN and admit.call_count == 1
    assert len(player.loads) == 1
    rt.stop()


def test_late_diagnostic_cannot_replace_newer_operation(setup):
    c, _, _, _, _ = setup
    trial = ListenMusicTrial(GoalAdapter.for_user_trial(c))
    trial.permit("我想听歌了", {}, "old")
    trial.permit("我想听歌了", {}, "new")
    before = trial.snapshot()
    trial._trace("old", route="B5", receipt="started")
    assert trial.snapshot() == before


def test_private_operation_identifier_is_not_logged(setup):
    c, _, _, _, _ = setup
    trial = ListenMusicTrial(GoalAdapter.for_user_trial(c))
    trial.permit("我想听歌了", {}, "https://private.invalid/sensitive")
    assert "private" not in trial.snapshot().operation_id
    assert len(trial.snapshot().operation_id) == 32


def test_status_listener_cannot_change_business_result_or_hold_trial_lock(setup):
    c, _, _, _, _ = setup
    trial = ListenMusicTrial(GoalAdapter.for_user_trial(c))
    def listener(state):
        assert trial._lock.acquire(blocking=False)
        trial._lock.release()
        raise RuntimeError("fixture observer failure")
    trial.set_listener(listener)
    assert trial.set_enabled(True).status is S.CONFIRMED
    assert trial.snapshot().enabled


@pytest.mark.parametrize("movie", [True, None])
def test_production_unknown_or_active_movie_never_routes_to_legacy(setup, monkeypatch, movie):
    c, player, _, _, _ = setup
    chart_source(c)
    c.state_store.set("movie", "playing", movie)
    rt, trial, _, _, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    production(trial, c)
    trial.set_enabled(True)
    with (patch.object(c, "recommend_music", side_effect=AssertionError("movie permission")),
          patch.object(c, "admit_music_goal", side_effect=AssertionError("movie permission"))):
        speak(rt, "我想听歌了")
        joined(workers)
    assert values[0][0].status is not S.CONFIRMED and not player.loads
    rt.stop()
