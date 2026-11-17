"""Deterministic shutdown vs MPV self-heal; no real library/network/player."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.unit.test_goal_admission import setup as setup


def done(controller):
    end = time.monotonic() + 1
    while controller._mpv_restart_pending and time.monotonic() < end:
        time.sleep(0.001)
    assert not controller._mpv_restart_pending, "restart worker must finish within bound"


def test_stopped_owner_rejects_a_poll_iteration_late_restart(setup):
    c, p, _, _, _ = setup
    p.start = MagicMock(return_value=True)
    c.stop()
    assert c._running is False
    c._try_restart_mpv("process-dead-after-stop")
    done(c)
    p.start.assert_not_called()
    assert c._mpv_restart_count == 0


def test_stop_during_pending_start_returns_with_no_resurrected_player(setup):
    c, p, _, _, _ = setup
    entered, release = threading.Event(), threading.Event()
    state = SimpleNamespace(alive=True)

    def start(**kwargs):
        entered.set()
        assert release.wait(1)
        state.alive = True
        return True

    p.start = MagicMock(side_effect=start)
    p.stop_process = lambda: setattr(state, "alive", False)
    c._try_restart_mpv("owned-process-died")
    assert entered.wait(1)
    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (c.stop(), stopped.set()))
    stopper.start()
    try:
        end = time.monotonic() + 0.5
        while c._running and time.monotonic() < end:
            time.sleep(0.001)
        assert c._running is False  # stop has invalidated the old lifecycle
    finally:
        release.set()
        stopper.join(1)
    assert not stopper.is_alive() and stopped.is_set()
    done(c)
    assert state.alive is False
    assert p.start.call_count == 1


def test_live_self_heal_still_starts_exactly_once(setup):
    c, p, _, _, _ = setup
    p.start = MagicMock(return_value=True)
    c._try_restart_mpv("live-process-dead")
    done(c)
    p.start.assert_called_once_with(taskkill=False, pipe_timeout=3.0, prop_timeout=0.5)
    assert c._mpv_restart_count == 1 and c._running


def test_restart_does_not_hold_business_lock_while_waiting_for_start(setup):
    c, p, song, _, _ = setup
    entered, release = threading.Event(), threading.Event()

    def start(**kwargs):
        entered.set()
        assert release.wait(1)
        return True

    p.start = MagicMock(side_effect=start)
    c._try_restart_mpv("slow-owned-start")
    try:
        assert entered.wait(1)
        begin = time.perf_counter()
        c.play_song(song)
        assert time.perf_counter() - begin < 0.2
        assert not p.loads
    finally:
        release.set()
    done(c)
