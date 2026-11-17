"""Same goal, original business, owned native MPV. Local chart fixture only."""

from dataclasses import asdict
import json
import os
import threading
import time
from unittest.mock import patch

import pytest

from app.music.goal_adapter import GoalAdapter
from tools.goal_execute_b3.lab import Lab
from tests.integration.test_goal_admission_mpv import run_worker

pytestmark = pytest.mark.integration


class OrderedLab(Lab):
    """Keep MPV alive until this lab's idle poller has exited.

    Legacy controller.stop does not join its poller before terminating MPV;
    this fixture does not claim to fix that production shutdown race.
    """

    def __exit__(self, *exc):
        alive = []
        try:
            if hasattr(self, "ctrl"):
                self.ctrl._running = False
                deadline = time.monotonic() + 2
                polls = [
                    t
                    for t in set(threading.enumerate()) - self.before_threads
                    if t.name == "music-idle-poll"
                ]
                for thread in polls:
                    thread.join(max(0, deadline - time.monotonic()))
                alive = [t.name for t in polls if t.is_alive()]
        finally:
            result = super().__exit__(*exc)
        assert not alive, alive
        return result


def charts(lab):
    source = lab.charts([lab.generate("chart-a"), lab.generate("chart-b")])
    source._plugins = [dict(name=source.name, status="ok", capabilities=dict(charts=True))]
    return source


@pytest.mark.parametrize(
    "scenario,plan",
    [
        ("playing", "NO_OP"),
        ("paused", "RESUME_MUSIC"),
        ("collection", "TRY_COLLECTION"),
        ("chart", "TRY_CHART"),
        ("both_local_preferred", "TRY_COLLECTION"),
        ("both_equal", "TRY_CHART"),
    ],
)
def test_goal_decision_admission_and_real_progress(tmp_path, scenario, plan):
    def body():
        with OrderedLab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            song, _ = lab.add_collection(lab.generate("collection"))
            if scenario.startswith("both") or scenario == "chart":
                charts(lab)
            if scenario == "both_local_preferred":
                c.db.update_song(song["id"], dict(artist="confirmed artist"))
                c.music_memory.set_preference("artist", "confirmed artist", 1)
            if scenario in ("playing", "paused"):
                c.play_song(song)
                end = time.monotonic() + 1
                ready = False
                while time.monotonic() < end:
                    sample = lab.port.sample(time.monotonic() + 0.3)
                    first, second = sample.get("time-pos"), sample.get("time-pos-after")
                    if (
                        sample.get("path") == song["path"]
                        and sample.get("idle-active") is False
                        and isinstance(first, (int, float))
                        and isinstance(second, (int, float))
                        and second > first
                    ):
                        ready = True
                        break
                assert ready, "fixture must establish actual progressing playback"
                if scenario == "paused":
                    c.pause()
            a = GoalAdapter(c, isolation_root=lab.root, enabled=True)
            before = c.queue.snapshot(c.get_current_song())
            with patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc:
                result = a.run(a.begin())
                assert result.decision.plan == plan, result
                assert result.status == "started", result
                assert result.receipt.proof.position > result.receipt.proof.previous_position
                loads = [x for x in rpc.call_args_list if x.args[0] == "loadfile"]
                assert len(loads) == (0 if scenario in ("playing", "paused") else 1)
            if scenario in ("playing", "paused"):
                assert c.queue.snapshot(c.get_current_song()) == before
            if plan == "TRY_CHART":
                assert c.queue.size() == 2 and c.recommendations.session
            assert lab.manual_revision == 0
            (tmp_path / "b52-proof.json").write_text(
                json.dumps(asdict(result), indent=2), encoding="utf8"
            )
            a.close()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)

    run_worker(body)


@pytest.mark.parametrize("movie", [True, None])
def test_movie_permission_never_controls_either_player(tmp_path, movie):
    def body():
        with OrderedLab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            lab.add_collection(lab.generate())
            c.state_store.set("movie", "playing", movie)
            a = GoalAdapter(c, isolation_root=lab.root, enabled=True)
            with patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc:
                result = a.run(a.begin())
            assert result.status == "needs_clarification"
            assert lab.movie_calls == 0
            assert not [x for x in rpc.call_args_list if x.args[0] in ("loadfile", "set_property")]

    run_worker(body)


def test_real_manual_change_during_source_read_and_new_request_never_overwrites(tmp_path):
    def body():
        with OrderedLab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            song, _ = lab.add_collection(lab.generate("manual"))
            source = charts(lab)
            source.release.clear()
            a = GoalAdapter(c, isolation_root=lab.root, enabled=True)
            values = []
            task = threading.Thread(target=lambda: values.append(a.run(a.begin())))
            task.start()
            try:
                assert source.entered.wait(1)
                newer = a.begin()
                c.play_song(song)
                assert a.run(newer).status in ("rejected_unavailable", "needs_verification")
                source.release.set()
                task.join(2)
                assert not task.is_alive()
                assert values[0].status == "rejected_stale"
                assert c.mpv.get_property("path") == song["path"]
                assert c.get_current_song()["id"] == song["id"]
            finally:
                source.release.set()
                task.join(2)
                a.close()

    run_worker(body)


def test_real_corrupt_media_acceptance_is_not_started_or_fallback(tmp_path):
    def body():
        with OrderedLab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            lab.add_collection(lab.generate("broken", broken=True))
            a = GoalAdapter(c, isolation_root=lab.root, enabled=True)
            with patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc:
                result = a.run(a.begin(), budget=0.4)
            assert result.status in ("unknown_effects", "rejected_stale")
            assert result.receipt.effects_started and not result.receipt.safe_to_fallback
            assert len([x for x in rpc.call_args_list if x.args[0] == "loadfile"]) == 1
            assert a.run(a.begin()).status == "needs_verification"

    run_worker(body)


def test_short_goal_deadline_stops_slow_chart_without_second_load(tmp_path):
    def body():
        with OrderedLab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", False)
            source = charts(lab)
            source.release.clear()
            a = GoalAdapter(c, isolation_root=lab.root, enabled=True)
            catalog = source.get_top_lists

            def held_catalog():
                source.entered.set()
                assert source.release.wait(2)
                return catalog()

            begin = time.perf_counter()
            with (
                patch.object(source, "get_top_lists", side_effect=held_catalog),
                patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc,
            ):
                result = a.run(a.begin(), budget=0.12)
            assert time.perf_counter() - begin < 0.8
            assert result.status == "unknown_effects" and result.receipt.effects_started
            assert not [x for x in rpc.call_args_list if x.args[0] == "loadfile"]
            assert source.entered.is_set() and source.calls.count("resolve") == 0
            assert a.run(a.begin()).reason == "previous_admission_unresolved"
            source.release.set()
            a.close()

    run_worker(body)
