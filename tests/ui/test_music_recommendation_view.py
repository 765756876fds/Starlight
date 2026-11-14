"""Recommendation controls reuse LatestTask and never perform networking on the GUI thread."""
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.core.operation_result import OperationResult, OperationStatus
from app.ui.widgets.music_discovery import MusicDiscovery


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def wait(predicate):
    for _ in range(100):
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


def test_controls_run_off_main_thread_and_keep_identity(app):
    worker_threads, feedback = [], []
    current = {"title": "甲歌", "artist": "甲"}
    def recommend(**kwargs):
        worker_threads.append(QThread.currentThread())
        return OperationResult(OperationStatus.CONFIRMED, "正在播放 · 实际榜单")
    def remember(*args):
        worker_threads.append(QThread.currentThread())
        feedback.append(args)
        return OperationResult(OperationStatus.CONFIRMED, "已记住你的偏好")
    music = SimpleNamespace(recommend_music=recommend, recommendation_feedback=remember,
        _current_song=current, _lock=threading.Lock())
    view = MusicDiscovery(music)
    try:
        QTest.mouseClick(view.recommend_button, Qt.LeftButton)
        wait(lambda: view.recommendation_status.text() == "正在播放 · 实际榜单")
        QTest.mouseClick(view.like_artist_button, Qt.LeftButton)
        music._current_song = {"title": "乙歌", "artist": "乙"}
        wait(lambda: bool(feedback))
        assert feedback == [("artist", "甲", True)]
        assert all(t != app.thread() for t in worker_threads)
        assert view.thread() == app.thread()
    finally:
        view.shutdown()
        view.deleteLater()


def test_close_ignores_late_completion(app):
    entered, release = threading.Event(), threading.Event()
    def read(**kwargs):
        entered.set()
        assert release.wait(1)
        return OperationResult(OperationStatus.CONFIRMED, "late result")
    view = MusicDiscovery(SimpleNamespace(recommend_music=read))
    QTest.mouseClick(view.recommend_button, Qt.LeftButton)
    assert entered.wait(1)
    view.shutdown()
    before = view.recommendation_status.text()
    release.set()
    wait(lambda: not view._recommend_task._running)
    assert view.recommendation_status.text() == before
    view.deleteLater()


def test_failure_keeps_retry_available(app):
    view = MusicDiscovery(SimpleNamespace(recommend_music=lambda **kw:
        OperationResult(OperationStatus.FAILED, "没有可播放歌曲，请切换音源")))
    try:
        QTest.mouseClick(view.recommend_button, Qt.LeftButton)
        wait(lambda: "没有可播放歌曲" in view.recommendation_status.text())
        assert view.recommend_button.isEnabled()
        assert view.change_chart_button.isEnabled()
    finally:
        view.shutdown()
        view.deleteLater()


def test_voice_result_event_updates_gui_and_unsubscribes(app):
    from app.core.event_bus import Event, EventBus
    bus = EventBus()
    view = MusicDiscovery(SimpleNamespace(event_bus=bus))
    worker = threading.Thread(target=lambda: bus.publish(Event(
        event_type="music.recommendation_changed", data={"message": "真实来源 / 当前榜单"})))
    worker.start()
    worker.join(1)
    wait(lambda: view.recommendation_status.text() == "真实来源 / 当前榜单")
    view.shutdown()
    bus.publish(Event(event_type="music.recommendation_changed", data={"message": "late"}))
    QTest.qWait(20)
    assert view.recommendation_status.text() == "真实来源 / 当前榜单"
    view.deleteLater()
