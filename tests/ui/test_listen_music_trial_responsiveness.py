"""Existing asynchronous voice dispatch must never wait for snapshot IO on Qt."""

import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.core.operation_result import OperationStatus as S
from tests.unit.test_goal_admission import setup as setup  # noqa: F401
from tests.unit.test_listen_music_trial import attach, joined, speak, prefer


def test_voice_entry_keeps_qt_responsive_during_bounded_read(setup, monkeypatch, tmp_path):
    c, _, _, _, _ = setup
    prefer(c)
    rt, trial, _, _, values, workers = attach(c, Path(c.db_path).parent, monkeypatch)
    trial.set_enabled(True)
    app = QApplication.instance()
    assert app is not None
    entered, release = threading.Event(), threading.Event()
    gui_thread = threading.get_ident()
    reads, ticks = [], []
    capture = trial.adapter.resources.capture

    def slow(*args):
        reads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return capture(*args)

    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(5)
    start = time.monotonic()
    try:
        with patch.object(trial.adapter.resources, "capture", side_effect=slow):
            speak(rt, "我想听歌了")
            dispatch_ms = (time.monotonic() - start) * 1000
            deadline = time.monotonic() + 1
            while (not entered.is_set() or len(ticks) < 15) and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.001)
            assert entered.is_set() and len(ticks) >= 15
            assert reads and all(thread != gui_thread for thread in reads)
            release.set()
            while any(worker.is_alive() for worker in workers) and time.monotonic() < deadline + 3:
                app.processEvents()
                time.sleep(0.001)
            joined(workers)
        assert values[0][1] is None and values[0][0].status is S.CONFIRMED
        (tmp_path / "voice-qt-response.json").write_text(json.dumps(dict(
            dispatch_ms=dispatch_ms, ticks=len(ticks),
            max_gap_ms=max((b - a) * 1000 for a, b in zip(ticks, ticks[1:])),
        )))
    finally:
        release.set()
        joined(workers)
        timer.stop()
        timer.timeout.disconnect()
        rt.stop()
        trial.adapter.close()
