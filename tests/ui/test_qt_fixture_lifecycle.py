"""The shared fixture owns new test windows, including hidden/close-ignored ones."""
import threading

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from tests.conftest import _qt_process_cleanup

pytestmark = pytest.mark.ui


def finish(cleanup):
    with pytest.raises(StopIteration):
        next(cleanup)


def drain():
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def test_fixture_destroys_new_hidden_windows_and_their_timers():
    app = QApplication.instance() or QApplication([])
    cleanup = _qt_process_cleanup.__wrapped__()
    next(cleanup)
    window = QWidget()
    timer = QTimer(window)
    timer.start(10000)
    destroyed = []
    window.destroyed.connect(lambda: destroyed.append(threading.get_ident()))
    finish(cleanup)
    assert not isValid(window) and not isValid(timer)
    assert destroyed == [threading.get_ident()]
    drain()
    assert len(destroyed) == 1


def test_fixture_destroys_a_test_window_that_only_hides_on_close():
    app = QApplication.instance() or QApplication([])

    class HideOnly(QWidget):
        def closeEvent(self, event):
            self.hide()
            event.ignore()

    cleanup = _qt_process_cleanup.__wrapped__()
    next(cleanup)
    window = HideOnly()
    window.show()
    finish(cleanup)
    assert not isValid(window)


def test_fixture_preserves_windows_owned_by_an_outer_fixture():
    app = QApplication.instance() or QApplication([])
    shared = QWidget()
    shared.show()
    cleanup = _qt_process_cleanup.__wrapped__()
    next(cleanup)
    temporary = QWidget()
    try:
        finish(cleanup)
        assert isValid(shared) and shared.isVisible()
        assert not isValid(temporary)
    finally:
        shared.deleteLater()
        drain()


def test_fixture_reports_a_live_test_thread_without_deleting_its_window():
    app = QApplication.instance() or QApplication([])
    release = threading.Event()

    class HeldThread(QThread):
        def run(self):
            release.wait(2)

    cleanup = _qt_process_cleanup.__wrapped__()
    next(cleanup)
    window = QWidget()
    thread = HeldThread(window)
    thread.start()
    try:
        with pytest.raises(AssertionError, match="running QThread"):
            next(cleanup)
        assert isValid(window) and thread.isRunning()
    finally:
        release.set()
        assert thread.wait(1000)
        window.deleteLater()
        drain()
