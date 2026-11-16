"""Explicit standalone observer: Qt remains responsive; no production hook."""
from dataclasses import replace
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from tools.goal_shadow.contracts import Goal
from tools.goal_shadow.observer import ShadowObserver
from tools.goal_shadow.readonly import off_gui_thread
from tests.unit.test_goal_shadow import world


def wait(app, predicate, timeout=1):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline and not predicate():
        app.processEvents()
        time.sleep(.002)
    assert predicate()


def test_slow_read_does_not_block_qt_or_create_extra_workers():
    app = QApplication.instance() or QApplication([])
    entered, release = threading.Event(), threading.Event()
    class Slow:
        def capture(self, budget):
            off_gui_thread()
            entered.set()
            assert release.wait(1)
            return world("playing")
        def generation(self):
            return (1,2)
    observer = ShadowObserver(Slow(), budget=.05)
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        assert observer.observe(Goal())
        wait(app, entered.is_set)
        assert all(observer.observe(Goal()) is False for _ in range(50))
        wait(app, lambda: len(ticks)>=15)
        assert observer.poll() is None
        release.set()
        wait(app, lambda: not observer._thread.is_alive())
        assert observer.poll() is None  # Late result never revives.
    finally:
        timer.stop()
        release.set()
        assert observer.close(timeout=.2)


def test_main_thread_read_is_rejected_and_exception_isolated():
    app = QApplication.instance() or QApplication([])
    try:
        off_gui_thread()
        raise AssertionError("GUI read was allowed")
    except RuntimeError as exc:
        assert str(exc) == "read_snapshot_requires_worker"
    class Broken:
        def capture(self, budget):
            raise RuntimeError("PRIVATE_PATH_OR_TITLE")
    observer = ShadowObserver(Broken())
    try:
        assert observer.observe(Goal())
        wait(app, lambda: observer.poll() is not None)
        assert observer.poll().reason == "observer_read_failed"
        assert "PRIVATE" not in repr(observer.poll())
    finally:
        assert observer.close()


def test_close_discards_pending_snapshot():
    app = QApplication.instance() or QApplication([])
    entered, release = threading.Event(), threading.Event()
    class Reader:
        def capture(self, budget):
            entered.set()
            release.wait(.5)
            return replace(world("playing"), consistent=True)
        def generation(self):
            return (1,2)
    observer = ShadowObserver(Reader())
    assert observer.observe(Goal())
    wait(app, entered.is_set)
    assert observer.close(timeout=0) is False
    release.set()
    wait(app, lambda: not observer._thread.is_alive())
    assert observer.poll() is None
    assert observer.observe(Goal()) is False
