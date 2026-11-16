"""No new default route; real chart admission waits off the GUI thread."""

import os
import json
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.music.goal_admission import GoalRequest, AdmissionStatus as S
from tools.goal_execute_b3.lab import Lab


def test_formal_admission_slow_source_keeps_qt_responsive(tmp_path):
    app = QApplication.instance() or QApplication([])
    shared, results, failures, controls = [], [], [], []

    def work():
        try:
            with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
                c = lab.ctrl
                c.state_store.set("movie", "playing", False)
                source = lab.charts([lab.generate("chart")])
                source.release.clear()
                shared.append(source)
                req = GoalRequest("ui", "TRY_CHART", c.goal_state(), time.monotonic() + 3)
                controls.append((c, req))
                results.append(c.admit_music_goal(req))
                end = time.monotonic() + 1
                while time.monotonic() < end:
                    value = c.observe_music_goal("ui")
                    if value.status is S.STARTED:
                        results.append(value)
                        break
                    time.sleep(0.02)
            results.append(lab.cleanup)
        except BaseException as exc:
            failures.append(exc)

    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    worker = threading.Thread(target=work)
    worker.start()
    held = None
    checked_gui = False
    try:
        end = time.monotonic() + 7
        while worker.is_alive() and time.monotonic() < end:
            app.processEvents()
            if shared and shared[0].entered.is_set():
                if not checked_gui:
                    begin = time.perf_counter()
                    result = controls[0][0].admit_music_goal(controls[0][1])
                    assert result.status is S.REJECTED_UNAVAILABLE
                    assert time.perf_counter() - begin < 0.1
                    checked_gui = True
                held = len(ticks) if held is None else held
                if len(ticks) - held >= 15:
                    shared[0].release.set()
            time.sleep(0.002)
        assert not worker.is_alive() and not failures, failures
        assert held is not None and len(ticks) - held >= 15
        assert checked_gui
        assert results[0].status is S.ACCEPTED and results[1].status is S.STARTED
        assert results[2] == dict(threads_remaining=[], process_exited=True)
        gaps = [1000 * (b - a) for a, b in zip(ticks, ticks[1:])]
        (tmp_path / "b4-qt-timing.json").write_text(
            json.dumps(dict(ticks=len(ticks), max_gap_ms=max(gaps))), encoding="utf8"
        )
    finally:
        if shared:
            shared[0].release.set()
        worker.join(2)
        timer.stop()
        assert not worker.is_alive()
