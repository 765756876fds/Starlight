"""One real 60-second offline gate: responsive Qt and no late playback."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QPushButton

from app.core.event_bus import EventType, EventBus
from app.core.operation_result import OperationStatus
from app.voice.runtime import VoiceRuntime
from tests.ui.test_music_queue_view import qapp as qapp
from tests.unit.test_music_recommendation import controller as controller


def test_sixty_second_source_and_observer_leave_qt_responsive_without_late_play(qapp, controller):
    c = controller
    gate, entered = threading.Event(), threading.Event()
    original = c.online_manager.get_top_lists
    calls, results, beats, clicks = [], [], [], []
    def slow():
        calls.append(time.monotonic())
        entered.set()
        assert gate.wait(65)
        return original()
    executor = ThreadPoolExecutor(max_workers=4)
    c.online_manager._executor = executor
    c.online_manager.get_top_lists = slow
    c.recommendations.READ_TIMEOUT = .05
    voice = VoiceRuntime({})
    voice.event_bus = EventBus()
    voice.event_bus.subscribe(EventType.VOICE_TEXT_RECEIVED, lambda event: gate.wait(65))
    voice.asr = Mock(transcribe=Mock(return_value="停一下"))
    voice._execute_intent = Mock()
    button = QPushButton("offline responsiveness probe")
    button.clicked.connect(lambda: clicks.append(time.monotonic()))
    timer = QTimer(button)
    timer.setInterval(25)
    def heartbeat():
        beats.append(time.monotonic())
        button.click()
    timer.timeout.connect(heartbeat)
    loop = QEventLoop()
    start = time.monotonic()
    release = threading.Timer(60, gate.set)
    def requests():
        for _ in range(12):
            results.append(c.recommend_music(change_chart=True))
    worker = threading.Thread(target=requests)
    try:
        release.start()
        worker.start()
        assert entered.wait(1)
        # Dispatch on GUI, with a passive subscriber that will wait a full minute.
        voice._process_speech(b"offline-probe", 16000)
        timer.start()
        QTimer.singleShot(60500, loop.quit)
        loop.exec()
        worker.join(1)
        assert not worker.is_alive() and gate.is_set()
        assert time.monotonic() - start >= 60
        assert len(calls) == 2, "stale requests must not consume all source-pool slots"
        assert len(results) == 12 and all(r.status != OperationStatus.CONFIRMED for r in results)
        assert any(r.status == OperationStatus.UNAVAILABLE and "上次音源" in r.message for r in results)
        assert not c.mpv.loaded and c.queue.size() == 0, "a late result must not start playback"
        voice._execute_intent.assert_called_once()
        assert len(beats) > 500 and len(clicks) == len(beats)
        assert max(b-a for a, b in zip(beats, beats[1:])) < 1.0
        c.online_manager.get_top_lists = original
        c.recommendations.READ_TIMEOUT = 1
        assert c.recommend_music(change_chart=True).status == OperationStatus.CONFIRMED
        assert c.queue.size() == 10
    finally:
        gate.set()
        release.cancel()
        release.join(1)
        worker.join(1)
        timer.stop()
        voice._observations.close()
        voice.event_bus.shutdown()
        executor.shutdown(wait=True, cancel_futures=True)
        button.deleteLater()
        qapp.processEvents()
