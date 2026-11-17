"""Real isolated MPV plus bounded slow source; no official main window."""

import json
import os
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.music.goal_adapter import GoalAdapter
from tests.integration.test_goal_adapter_mpv import charts, OrderedLab


def test_slow_source_keeps_gui_responsive_and_worker_exits(tmp_path):
    app = QApplication.instance()
    assert app is not None
    shared, results, errors = [], [], []

    def work():
        try:
            with OrderedLab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
                lab.ctrl.state_store.set("movie", "playing", False)
                source = charts(lab)
                source.release.clear()
                a = GoalAdapter(lab.ctrl, isolation_root=lab.root, enabled=True)
                ticket = a.begin()
                shared.append((a, source, ticket))
                results.append(a.run(ticket))
                a.close()
            results.append(lab.cleanup)
        except BaseException as e:
            errors.append(e)

    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    thread = threading.Thread(target=work)
    thread.start()
    held = None
    checked_gui = False
    try:
        end = time.monotonic() + 7
        while thread.is_alive() and time.monotonic() < end:
            app.processEvents()
            if shared and shared[0][1].entered.is_set():
                if not checked_gui:
                    started = time.perf_counter()
                    assert shared[0][0].prepare(shared[0][2]).status == "needs_verification"
                    assert time.perf_counter() - started < 0.1
                    checked_gui = True
                held = len(ticks) if held is None else held
                if len(ticks) - held >= 15:
                    shared[0][1].release.set()
            time.sleep(0.002)
        assert not thread.is_alive() and not errors, errors
        assert held is not None and len(ticks) - held >= 15
        assert checked_gui
        assert results[0].status == "started"
        assert results[1] == dict(threads_remaining=[], process_exited=True)
        (tmp_path / "b52-qt.json").write_text(
            json.dumps(
                dict(
                    ticks=len(ticks),
                    max_gap_ms=max((b - a) * 1000 for a, b in zip(ticks, ticks[1:])),
                )
            ),
            encoding="utf8",
        )
    finally:
        if shared:
            shared[0][1].release.set()
        thread.join(2)
        timer.stop()
        assert not thread.is_alive()
