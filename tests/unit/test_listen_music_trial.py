"""Existing text/router/bus/controller chain; fake IPC, temporary DB only."""

from pathlib import Path
import threading
from unittest.mock import Mock, patch

import pytest

from app.core import notifications as notices
from app.core.command_bus import Command, CommandType as C
from app.core.notifications import NotificationCenter
from app.core.operation_result import OperationResult, OperationStatus as S
from app.music.goal_adapter import AdapterResult, GoalAdapter
from app.music.goal_admission import AdmissionStatus, GoalReceipt
from app.voice.listen_music_trial import ListenMusicTrial
from app.voice.runtime import VoiceRuntime
from tests.unit.test_goal_admission import setup as setup  # noqa: F401


def attach(c, root, monkeypatch):
    center = NotificationCenter()
    monkeypatch.setattr(notices, "_center", center)
    runtime = VoiceRuntime({})  # Constructs no running ASR/microphone service.
    runtime.command_bus = c.command_bus
    runtime._enqueue_speak = Mock()
    adapter = GoalAdapter(c, isolation_root=root, enabled=True)
    trial = ListenMusicTrial(adapter)
    runtime.bind_listen_music_trial(trial)
    commands, results, workers = [], [], []
    original = c.command_bus.execute_async

    def dispatch(command, on_done=None):
        commands.append(command)

        def done(result, error):
            results.append((result, error))
            if on_done:
                on_done(result, error)

        worker = original(command, on_done=done)
        workers.append(worker)
        return worker

    monkeypatch.setattr(c.command_bus, "execute_async", dispatch)
    return runtime, trial, center, commands, results, workers


def prefer(c):
    c.db.update_song(1, {"artist": "fixture preferred"})
    c.music_memory.set_preference("artist", "fixture preferred", 1)


def speak(runtime, text):
    intent = runtime.intent_router.parse(text)
    runtime._execute_intent(intent, text)
    return intent


def joined(workers):
    for worker in workers:
        worker.join(6)
        assert not worker.is_alive(), "bounded command worker must exit"


def test_uninstalled_and_disabled_are_identical_legacy_result_and_arguments(setup, monkeypatch):
    c, _, _, _, _ = setup
    sentinel = OperationResult(S.ACCEPTED, "legacy unchanged")
    old = Mock(return_value=sentinel)
    monkeypatch.setattr(c, "recommend_music", old)
    command = Command(C.MUSIC_RECOMMEND, source="voice", is_current=lambda: True)
    assert c.command_bus.execute(command) is sentinel
    rt, trial, _, commands, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    with patch.object(trial.adapter, "run", side_effect=AssertionError("default off")):
        speak(rt, "我想听歌了")
        joined(workers)
    assert commands[0].goal_trial is None
    assert values == [(sentinel, None)] and old.call_count == 2
    assert old.call_args.kwargs == {"is_current": commands[0].is_current}


@pytest.mark.parametrize("text", ["放点歌听听", "放点我喜欢的", "换个榜单", "换个风格", "来点伤感的"])
def test_enabled_other_existing_recommendations_keep_legacy(setup, monkeypatch, text):
    c, _, _, _, _ = setup
    rt, trial, _, commands, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    old = Mock(return_value=OperationResult(S.ACCEPTED, "legacy"))
    monkeypatch.setattr(c, "recommend_music", old)
    trial.set_enabled(True)
    with patch.object(trial.adapter, "run", side_effect=AssertionError("not whitelisted")):
        assert speak(rt, text).intent_type == C.MUSIC_RECOMMEND
        joined(workers)
    assert commands[0].goal_trial is None and old.call_count == 1
    assert values[0][1] is None


@pytest.mark.parametrize("text", ["不要我想听歌了", "他说我想听歌了", "为什么我想听歌了", "我想听歌了然后看电影"])
def test_safety_priority_does_not_grant_permit(setup, monkeypatch, text):
    c, _, _, _, _ = setup
    rt, trial, _, _, _, _ = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    assert trial.permit(text, {}) is None
    assert rt.intent_router.parse(text).intent_type != C.MUSIC_RECOMMEND


def test_chart_uses_one_pre_admission_legacy_route(setup, monkeypatch):
    from tests.unit.test_goal_adapter import chart_source

    c, p, _, _, _ = setup
    chart_source(c)
    rt, trial, center, commands, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    legacy = OperationResult(S.ACCEPTED, "original recommendation receipt")
    with (patch.object(c, "recommend_music", return_value=legacy) as old,
          patch.object(c, "admit_music_goal", side_effect=AssertionError("no B4 attempt"))):
        speak(rt, "我想听歌了。")
        joined(workers)
    assert values == [(legacy, None)] and old.call_count == 1
    assert old.call_args.kwargs["expected_request"] == 0
    assert not c._goal_admission._records and not p.loads
    assert commands[0].goal_trial is not None and center.history()[0].status != "success"


@pytest.mark.parametrize("action", ["disable", "new_voice", "manual", "shutdown"])
def test_slow_preparation_invalidation_and_bounded_drain(setup, monkeypatch, action):
    c, p, song, _, _ = setup
    prefer(c)
    rt, trial, center, _, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    entered, release = threading.Event(), threading.Event()
    prepare = trial.adapter.prepare

    def slow(ticket):
        prepared = prepare(ticket)
        entered.set()
        assert release.wait(2)
        return prepared

    old = Mock(return_value=OperationResult(S.ACCEPTED, "later explicit request"))
    monkeypatch.setattr(c, "recommend_music", old)
    with patch.object(trial.adapter, "prepare", side_effect=slow):
        try:
            speak(rt, "我想听歌了")
            assert entered.wait(1)
            if action == "disable":
                assert trial.set_enabled(False).status is S.ACCEPTED
                speak(rt, "我想听歌了")  # Busy, not a duplicate legacy attempt.
            elif action == "new_voice":
                speak(rt, "放点歌听听")
            elif action == "manual":
                c.play_song(song)
            else:
                rt.stop()
            release.set()
            joined(workers)
        finally:
            release.set()
            joined(workers)
    assert old.call_count == (1 if action == "new_voice" else 0)
    assert len(p.loads) == (1 if action == "manual" else 0)
    assert all(value.status is not S.CONFIRMED and error is None for value, error in values)
    assert all(record.status != "success" for record in center.history())
    trial.set_enabled(False)
    speak(rt, "我想听歌了")
    joined(workers)
    assert old.call_count == (2 if action == "new_voice" else 1)
    assert values[-1][0].status is S.ACCEPTED


def test_unknown_effects_never_retries_and_notification_ids_do_not_cross(setup, monkeypatch):
    c, p, _, _, _ = setup
    prefer(c)
    rt, trial, center, commands, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    # Simulates an ambiguous IPC receipt, not native MPV playback evidence.
    outcome = AdapterResult("unknown_effects", "decode_not_confirmed", receipt=GoalReceipt(
        "isolated", AdmissionStatus.UNKNOWN_EFFECTS, "decode_not_confirmed", True, True))
    with patch.object(trial.adapter, "run", return_value=outcome) as run:
        with patch.object(c, "recommend_music", side_effect=AssertionError("no automatic fallback")):
            speak(rt, "我想听歌了")
            joined(workers)
            trial.set_enabled(False)
    assert run.call_count == 1 and values[0][0].status is S.UNKNOWN
    record, = center.history()
    assert record.id == commands[0].notification_id and record.status != "success"
    assert not p.loads


def test_accepted_disable_waits_and_reenable_preserves_ambiguous_admission(setup, monkeypatch):
    c, p, _, _, _ = setup
    prefer(c)
    rt, trial, center, commands, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    p.progress = False
    entered, release = threading.Event(), threading.Event()
    original = trial.adapter.observe

    def hold(ticket):
        entered.set()
        assert release.wait(2)
        return original(ticket)

    with patch.object(c, "recommend_music", side_effect=AssertionError("no retry")):
        with patch.object(trial.adapter, "observe", side_effect=hold):
            try:
                speak(rt, "我想听歌了")
                assert entered.wait(1) and len(p.loads) == 1
                assert trial.set_enabled(False).status is S.ACCEPTED
                speak(rt, "我想听歌了")  # Cannot use V1 while prior admission drains.
                release.set()
                joined(workers)
            finally:
                release.set()
                joined(workers)
        assert trial.adapter._pending is not None
        trial.set_enabled(True)
        speak(rt, "我想听歌了")
        joined(workers)
    assert len(p.loads) == 1 and len(values) == 3
    assert all(result.status is not S.CONFIRMED and error is None for result, error in values)
    assert values[-1][0].outcome.reason == "previous_admission_unresolved"
    assert len({command.notification_id for command in commands}) == 3
    assert all(record.status != "success" for record in center.history())


@pytest.mark.parametrize("state,status", [("accepted", S.ACCEPTED), ("started", S.UNKNOWN),
                                          ("unknown_effects", S.UNKNOWN), ("rejected_stale", S.UNKNOWN)])
def test_no_start_confirmation_without_proof(state, status):
    result = ListenMusicTrial._feedback(AdapterResult(state, "isolated"))
    assert result.status is status


def test_binding_cannot_be_replaced_or_moved_to_another_bus(setup, monkeypatch):
    c, _, _, _, _ = setup
    rt, trial, _, _, _, _ = attach(c, Path(c.db_path).parent, monkeypatch)
    other = ListenMusicTrial(trial.adapter)
    with pytest.raises(ValueError):
        rt.bind_listen_music_trial(other)
    second = VoiceRuntime({})
    with pytest.raises(ValueError):
        second.bind_listen_music_trial(trial)


def test_nontrial_change_chart_keeps_legacy_even_while_trial_drains(setup, monkeypatch):
    c, p, _, _, _ = setup
    prefer(c)
    rt, trial, _, commands, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    entered, release = threading.Event(), threading.Event()
    prepare = trial.adapter.prepare
    legacy = OperationResult(S.ACCEPTED, "explicit legacy chart request")
    old = Mock(return_value=legacy)
    monkeypatch.setattr(c, "recommend_music", old)

    def hold(ticket):
        prepared = prepare(ticket)
        entered.set()
        assert release.wait(2)
        return prepared

    with patch.object(trial.adapter, "prepare", side_effect=hold):
        try:
            speak(rt, "我想听歌了")
            assert entered.wait(1)
            speak(rt, "换个榜单")
            workers[-1].join(1)
            assert not workers[-1].is_alive()
            old.assert_called_once_with(is_current=commands[-1].is_current, change_chart=True)
            assert values[0] == (legacy, None)
        finally:
            release.set()
            joined(workers)
    assert commands[-1].goal_trial is None
    assert values[-1][0].status is S.UNKNOWN and not p.loads
