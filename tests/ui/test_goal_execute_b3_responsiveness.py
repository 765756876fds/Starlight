"""Real isolated business/MPV with a slow local resolver and a live Qt loop."""
import os
import json
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from tools.goal_execute_b3.execution import Executor
from tools.goal_execute_b3.lab import Lab
from tools.goal_shadow_b2.decision import Outcome


def test_real_chart_wait_does_not_block_gui_and_resources_close(tmp_path):
    app = QApplication.instance() or QApplication([])
    box = QWidget()
    box.setWindowTitle("B3 isolated responsiveness probe")
    box.show()
    shared, results, failures = [], [], []
    def run():
        try:
            with Lab(tmp_path / "isolated", os.environ["STARLIGHT_TEST_MPV"]) as lab:
                path = lab.generate()
                source = lab.charts([path])
                source.release.clear()
                shared.append(source)
                engine = Executor(lab)
                result = engine.execute(engine.capture())
                results.append(result)
            results.append(lab.cleanup)
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=run, name="b3-ui-background")
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    worker.start()
    try:
        deadline = time.monotonic()+8
        held_ticks = None
        released_ticks = None
        while worker.is_alive() and time.monotonic()<deadline:
            app.processEvents()
            if shared and shared[0].entered.is_set():
                if held_ticks is None:
                    held_ticks = len(ticks)
                if len(ticks)-held_ticks >= 15 and released_ticks is None:
                    released_ticks = len(ticks)
                    shared[0].release.set()
            time.sleep(.002)
        assert not worker.is_alive() and not failures, failures
        assert held_ticks is not None and len(ticks)-held_ticks >= 15
        assert results[0].status is Outcome.STARTED
        assert results[1] == {"threads_remaining": [], "process_exited": True}
        gaps = [1000*(b-a) for a,b in zip(ticks, ticks[1:])]
        (tmp_path / "qt-timing.json").write_text(json.dumps(dict(ticks=len(ticks),
            blocked_resolver_ticks=released_ticks-held_ticks,
            maximum_heartbeat_gap_ms=max(gaps))), encoding="utf8")
    finally:
        if shared:
            shared[0].release.set()
        worker.join(3)
        timer.stop()
        box.close()
        assert not worker.is_alive()
