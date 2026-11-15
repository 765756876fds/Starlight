"""A slow offline source must leave the real Qt event loop responsive."""

import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest

from app.voice.runtime import VoiceRuntime
from tests.unit.test_voice_next_feedback import controller as controller, setup as setup  # noqa: F401
from tests.ui.test_music_queue_view import qapp as qapp  # noqa: F401


def test_voice_next_async_keeps_gui_responsive(qapp, setup):
    c, center, _ = setup
    c._next_feedback.budget = 2
    runtime = VoiceRuntime({})
    runtime.command_bus = c.command_bus
    entered, release = threading.Event(), threading.Event()
    old = c.online_manager.get_play_url
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(1.5)
        return old(*args, **kwargs)
    c.online_manager.get_play_url = delayed
    pulses = []
    try:
        start = time.monotonic()
        runtime._execute_intent(runtime.intent_router.parse("下一首"), "下一首")
        assert time.monotonic() - start < 0.2
        deadline = time.monotonic() + 1
        while not entered.is_set() and time.monotonic() < deadline:
            QTest.qWait(10)
            time.sleep(0.005)  # Also let Python source threads run between Qt turns.
        assert entered.is_set()
        QTimer.singleShot(20, lambda: pulses.append(not release.is_set()))
        QTest.qWait(100)
        assert entered.is_set() and pulses == [True]
        assert center.history()[0].status == "processing"
    finally:
        release.set()
    deadline = time.monotonic() + 1
    while center.history()[0].status == "processing" and time.monotonic() < deadline:
        QTest.qWait(10)
        time.sleep(0.005)
    assert center.history()[0].status == "success", center.history()[0].title
