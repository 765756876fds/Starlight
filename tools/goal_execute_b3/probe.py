"""Reproducible native scenarios. Every file/player belongs to the new Lab."""
from dataclasses import asdict
import json
from pathlib import Path
import threading
import time

from tools.goal_shadow.contracts import Goal
from tools.goal_shadow_b2.decision import Action, Outcome
from .execution import Executor
from .lab import Lab, LocalSource

SCENARIOS = ("A", "B", "C", "D", "E", "F", "G_before", "G_during", "G_member", "G_budget",
             "G_source", "H_safe", "H_unknown", "H_queue", "I_playing", "I_unknown")


def wait(predicate, seconds=2):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("bounded_native_probe_wait_expired")


def ready(executor, wanted="playing"):
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        result = executor.capture()
        if result.world.music == wanted:
            return result
        time.sleep(.02)
    raise AssertionError("decoder_state_not_reliably_observed")


def run_case(name, output, binary):
    if name not in SCENARIOS:
        raise ValueError("scenario_not_allowlisted")
    output = Path(output)
    if output.exists():
        raise ValueError("first_native_evidence_must_not_be_overwritten")
    output.mkdir(parents=True)
    report = {"scenario": name, "goal": "LISTEN_MUSIC", "network": "none",
              "real_mpv": True, "user_media_access": False}
    lab = Lab(output / "state", binary)
    try:
        with lab:
            engine = Executor(lab)
            track = lab.generate("生成静音")
            song = pid = None
            if name in ("A", "B", "C", "F", "G_before", "G_member", "H_safe", "H_unknown", "H_queue"):
                song, pid = lab.add_collection(lab.generate("损坏", broken=True) if name == "F" else track)
            if name in ("D", "G_during", "G_source", "G_budget", "H_safe", "H_unknown", "H_queue"):
                lab.charts([track, lab.generate("榜单第二首")])
            if name in ("A", "B"):
                lab.manual(lambda: lab.ctrl.play_song(song))
                ready(engine)
                if name == "B":
                    lab.manual(lab.ctrl.pause)
                    ready(engine, "paused")
            if name.startswith("I_"):
                lab.movie = name.removeprefix("I_")
                if name == "I_playing":
                    lab.start_movie_probe(track)
            observed = engine.capture(Goal())
            report["world"] = dict(music=observed.world.music, movie=observed.world.movie,
                generation=observed.world.generation, inventory=observed.world.inventory,
                capabilities=[dict(provider=c.provider, level=c.level.value, basis=c.basis)
                              for c in observed.world.capabilities])
            report["decision"] = dict(action=observed.decision.action.value, reason=observed.decision.reason,
                                      candidates=[p.action.value for p in observed.decision.candidates])
            report["timing"] = dict(snapshot_ms=observed.snapshot_ms, decision_ms=observed.decision_ms)
            loads = len(lab.mpv.loads)
            queue = lab.ctrl.queue.snapshot(lab.ctrl.get_current_song())
            membership_before = lab.ctrl.db.get_playlist_songs(pid) if pid else []
            db_changes_before = lab.ctrl.db._conn.total_changes
            if name == "G_before":
                other, _ = lab.add_collection(lab.generate("手动新内容"))
                lab.manual(lambda: lab.ctrl.play_song(other))
                ready(engine)
                loads = len(lab.mpv.loads)
                membership_before = lab.ctrl.db.get_playlist_songs(pid)
            if name == "G_member":
                member = lab.ctrl.db.get_playlist_songs(pid)[0]
                assert lab.ctrl.db.remove_from_playlist(pid, member["id"])
                membership_before = []
            if name == "G_source":
                replacement = LocalSource([track])
                lab.ctrl.online_manager.register(replacement)
            if name == "H_safe":
                lab.refuse_collection_once = True
            if name == "H_unknown":
                lab.mpv.set_property("pause", True)
                lab.mpv.unknown_after_send = True
            if name == "H_queue":
                lab.mpv.fail_before_send = True
            if name == "G_budget":
                lab.source.release.clear()
            if name == "G_during":
                source = lab.source
                source.release.clear()
                answers, errors = [], []
                def execute():
                    try:
                        answers.append(engine.execute(observed))
                    except BaseException as exc:
                        errors.append(exc)
                task = threading.Thread(target=execute, name="b3-goal-request")
                task.start()
                try:
                    assert source.entered.wait(1)
                    other, _ = lab.add_collection(lab.generate("后来的手动选择"))
                    lab.manual(lambda: lab.ctrl.play_song(other))
                    manual_loads = len(lab.mpv.loads)
                    source.release.set()
                    task.join(2)
                    assert not task.is_alive() and not errors, errors
                    result = answers[0]
                    assert result.status is Outcome.STALE
                    assert len(lab.mpv.loads) == manual_loads
                    assert lab.ctrl.get_current_song()["id"] == other["id"]
                finally:
                    source.release.set()
                    task.join(2)
            else:
                result = engine.execute(observed, budget=.15 if name == "G_budget" else 4)
            report["result"] = asdict(result)
            report["mpv_confirmation"] = engine.last_confirmation
            report["business_return"] = engine.last_business_return
            report["db_connection_changes"] = lab.ctrl.db._conn.total_changes - db_changes_before
            report["snapshot_observed_progress"] = dict(position=observed.position,
                position_after=observed.props.get("time-pos-after"), paused=observed.props.get("pause"))
            report["loads_after_decision"] = lab.mpv.loads[loads:]
            report["queue_changed"] = queue != lab.ctrl.queue.snapshot(lab.ctrl.get_current_song())
            report["membership_preserved"] = (not pid or
                                             membership_before == lab.ctrl.db.get_playlist_songs(pid))
            report["movie_pause_or_stop_calls"] = 0  # No such method exists in this fixture.
            report["independent_movie_mpv_still_playing"] = (
                lab.observe_movie() == "playing" if name == "I_playing" else None)
            report["movie_absence_checks"] = lab.movie_calls
            report["source_calls"] = lab.source.calls if lab.source else []
            if name in ("A", "B", "C", "D", "H_safe"):
                expected = {"A": Action.NO_OP, "B": Action.RESUME_MUSIC, "C": Action.TRY_COLLECTION,
                            "D": Action.TRY_CHART, "H_safe": Action.TRY_COLLECTION}[name]
                assert observed.decision.action is expected
                assert result.status is Outcome.STARTED and result.goal_satisfied, result
                if name in ("A", "B"):
                    assert not report["loads_after_decision"] and not report["queue_changed"]
                    assert report["db_connection_changes"] == 0
                if name == "H_safe":
                    assert result.attempts == 2
                if name == "D":
                    assert lab.ctrl.queue.size() == 2 and lab.ctrl.recommendations.session
            elif name == "E":
                assert observed.decision.action is Action.UNAVAILABLE
                assert not result.goal_satisfied and not report["loads_after_decision"]
                assert report["db_connection_changes"] == 0
            elif name == "F":
                assert observed.decision.action is Action.TRY_COLLECTION
                assert result.status in (Outcome.FAILED, Outcome.STALE) and not result.goal_satisfied
            elif name == "G_budget":
                assert result.status is Outcome.UNKNOWN and result.reason == "total_budget_expired"
                assert result.attempts == 1 and not report["loads_after_decision"]
            elif name.startswith("G_"):
                assert result.status is Outcome.STALE and not result.goal_satisfied
                if name != "G_during":
                    assert not report["loads_after_decision"]
            elif name.startswith("H_"):
                assert result.status is Outcome.UNKNOWN and result.attempts == 1
                assert not result.goal_satisfied and "resolve" not in report["source_calls"]
                assert len(report["loads_after_decision"]) == 1
            elif name.startswith("I_"):
                assert observed.decision.action is Action.NEEDS_CLARIFICATION
                assert not result.goal_satisfied and not report["loads_after_decision"]
                assert report["db_connection_changes"] == 0
                if name == "I_playing":
                    assert report["independent_movie_mpv_still_playing"] is True
            assert report["membership_preserved"]
    finally:
        report["cleanup"] = lab.cleanup
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    return report
