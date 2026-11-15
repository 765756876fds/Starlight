"""Real Qt timers detect blocking callbacks, using bounded offline backend gates."""
import threading
from types import SimpleNamespace

from PySide6.QtCore import QTimer, QThread
from PySide6.QtTest import QTest

from app.core.operation_result import OperationResult, OperationStatus
from app.ui.panels.music_panel import MusicPanel
from app.ui.widgets.music_discovery import MusicDiscovery
from tests.ui.test_music_queue_view import qapp as qapp, setup_view as setup_view

START_POLL = MusicPanel._start_progress_poll


def test_periodic_refresh_cannot_wait_for_volume_ipc(setup_view):
    s = setup_view
    release = threading.Event()
    observed = []
    s.mpv.get_volume.side_effect = lambda: (release.wait(.7), 70)[1]
    rescue = threading.Timer(.7, release.set)
    rescue.start()
    QTimer.singleShot(0, s.panel._refresh)
    QTimer.singleShot(30, lambda: observed.append(not release.is_set()))
    try:
        QTest.qWait(100)
        assert observed == [True], "GUI timer waited behind synchronous volume IPC"
        s.mpv.get_volume.assert_not_called()
    finally:
        release.set()
        rescue.cancel()
        rescue.join(1)


def test_preference_capture_does_not_wait_for_playback_lock(qapp):
    release = threading.Event()
    lock = threading.Lock()
    lock.acquire()
    music = SimpleNamespace(_lock=lock, _current_song={"title": "fixture", "artist": "fixture"},
        recommendation_feedback=lambda *a: OperationResult(OperationStatus.CONFIRMED, "已记住"))
    view = MusicDiscovery(music)
    observed = []
    def rescue():
        release.set()
        lock.release()
    timer = threading.Timer(.7, rescue)
    timer.start()
    QTimer.singleShot(0, lambda: view._remember_preference(True))
    QTimer.singleShot(30, lambda: observed.append(not release.is_set()))
    try:
        QTest.qWait(100)
        assert observed == [True], "GUI waited for the controller's playback lock"
    finally:
        timer.join(1)
        view.shutdown()
        view.deleteLater()
        qapp.processEvents()


def test_background_volume_is_queued_and_stale_gesture_is_ignored(setup_view):
    s = setup_view
    entered, release = threading.Event(), threading.Event()
    threads = []
    def volume():
        threads.append(QThread.currentThread())
        entered.set()
        assert release.wait(2)
        return 27
    s.mpv.get_volume.side_effect = volume
    s.mpv.get_duration.return_value = 180.0
    START_POLL(s.panel)
    try:
        assert entered.wait(1)
        s.panel._volume_request_gen += 1  # A later user gesture owns its display.
        s.panel._syncing_volume = True
        s.panel.slider_vol.setValue(80)
        s.panel._syncing_volume = False
        release.set()
        QTest.qWait(100)
        assert s.panel.slider_vol.value() == 80
        assert threads and all(thread != s.panel.thread() for thread in threads)
    finally:
        s.panel._progress_polling = False
        release.set()
        s.panel._progress_thread.join(2)
        assert not s.panel._progress_thread.is_alive()
