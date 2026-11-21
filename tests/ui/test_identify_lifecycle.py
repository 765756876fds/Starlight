"""Real QThreads with controlled completion; no capture, network or user audio."""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from app.ui.dialogs import identify_dialog as module

pytestmark = pytest.mark.ui


def drain():
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


def wait_until(check):
    deadline = time.monotonic() + 2
    while not check() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert check(), "bounded Qt completion wait expired"


@pytest.fixture
def scene(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    owned, gates = [], []

    def make(service):
        dialog = module.IdentifyDialog(service)
        owned.append(dialog)
        return dialog

    yield SimpleNamespace(app=app, make=make, gates=gates)
    for gate in gates:
        gate.set()
    for dialog in owned:
        for name in ("_identify_thread", "_online_thread", "_build_thread"):
            thread = getattr(dialog, name)
            if thread is not None:
                thread.quit()
                assert thread.wait(1000), "test-owned thread did not finish"
        if dialog._timer:
            dialog._timer.stop()
        dialog._cleanup_wav()
        dialog.hide()
        dialog.deleteLater()
    drain()


@pytest.mark.parametrize("outcome", ["match", "capture_error", "cancelled", "build", "build_error"])
def test_workers_finish_and_delete_in_their_thread_without_gui_dispatch(scene, outcome):
    started, release = threading.Event(), threading.Event()
    scene.gates.append(release)

    def read(*args, **kwargs):
        started.set()
        assert release.wait(2), "test completion gate was not released"
        if outcome in ("capture_error", "build_error"):
            raise RuntimeError("controlled failure")
        if outcome == "cancelled":
            raise module.CaptureCancelled()
        return ({"indexed": 1, "skipped": 0, "failed": 0} if outcome == "build"
                else {"title": "生成测试", "filepath": "unused.wav"})

    service = SimpleNamespace(record_seconds=1, has_library=lambda: True,
                              record_and_identify=read, index_pending=read)
    dialog = scene.make(service)
    building = outcome.startswith("build")
    dialog._show_build() if building else dialog.begin()
    thread = dialog._build_thread if building else dialog._identify_thread
    worker = dialog._build_worker if building else dialog._identify_worker
    destroyed = []
    worker.destroyed.connect(
        lambda *_: destroyed.append((QThread.currentThread() == thread,
                                     QThread.currentThread() == scene.app.thread())),
        Qt.DirectConnection)
    assert started.wait(1)
    release.set()
    # Intentionally no GUI pumping: a thread must finish during GUI shutdown,
    # while the result callback remains queued for the GUI thread.
    assert thread.wait(1000), "quit still depends on dispatching GUI events"
    assert dialog.is_busy(), "business UI callback ran outside the GUI thread"
    assert not isValid(worker), "worker survived its owning thread"
    assert destroyed == [(True, False)]
    wait_until(lambda: not dialog.is_busy())
    if outcome == "match":
        assert "生成测试" in dialog.lbl_title.text()
    elif outcome == "capture_error":
        assert "失败" in dialog.lbl_status.text()
    elif outcome == "cancelled":
        assert "取消" in dialog.lbl_status.text()
    elif outcome == "build_error":
        assert "失败" in dialog.lbl_status.text()
    else:
        assert "新增 1 首" in dialog.lbl_status.text()


def test_online_worker_deletes_before_queued_result_is_rendered(scene, monkeypatch):
    started, release = threading.Event(), threading.Event()
    scene.gates.append(release)

    def recognize(path):
        started.set()
        assert release.wait(2)
        return module.ShazamRecognitionResult(module.ShazamRecognitionStatus.NO_MATCH)

    monkeypatch.setattr("app.music.shazamio_provider.ShazamIOProvider",
                        lambda: SimpleNamespace(recognize=recognize))
    dialog = scene.make(SimpleNamespace(record_seconds=1, has_library=lambda: False,
                                        capture_audio=lambda *a, **k: None))
    dialog.begin()
    wait_until(lambda: dialog._online_thread is not None and started.is_set())
    thread, worker = dialog._online_thread, dialog._online_worker
    destroyed = []
    worker.destroyed.connect(
        lambda *_: destroyed.append(QThread.currentThread() == thread), Qt.DirectConnection)
    release.set()
    assert thread.wait(1000)
    assert dialog.is_busy()
    assert not isValid(worker)
    assert destroyed == [True]
    wait_until(lambda: not dialog.is_busy())
    assert "未找到匹配" in dialog.lbl_status.text()
    assert dialog._current_wav is None


def test_close_during_capture_hides_without_destroying_live_thread(scene):
    started, release = threading.Event(), threading.Event()
    scene.gates.append(release)

    def read(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return {"title": "生成测试"}

    dialog = scene.make(SimpleNamespace(record_seconds=1, has_library=lambda: True,
                                        record_and_identify=read))
    dialog.show()
    dialog.begin()
    thread = dialog._identify_thread
    assert started.wait(1)
    dialog.close()
    assert not dialog.isVisible()
    assert thread.isRunning() and isValid(dialog)
    release.set()
    assert thread.wait(1000)
    wait_until(lambda: not dialog.is_busy())
