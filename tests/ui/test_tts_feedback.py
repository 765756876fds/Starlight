from types import SimpleNamespace
from unittest.mock import Mock
import time
import pytest

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from app.speech.tts import TTSState
from app.ui.panels.status_bar import StatusBar
from app.core import notifications


@pytest.fixture
def notices(monkeypatch):
    center = notifications.NotificationCenter()
    monkeypatch.setattr(notifications, "_center", center)
    return center


def test_tts_failure_is_visible_and_coalesced_without_hiding_capsule(monkeypatch, notices):
    app = QApplication.instance() or QApplication([])
    tts = SimpleNamespace(get_state=lambda: TTSState.IDLE, _generation=1)
    voice = SimpleNamespace(is_mic_on=lambda: False, tts_provider=lambda: tts)
    bar = StatusBar({"voice": voice})
    bar._timer.stop()
    def assert_gui(*args):
        assert QThread.currentThread() == QApplication.instance().thread()
    show = Mock(side_effect=assert_gui)
    notices.subscribe(assert_gui)
    monkeypatch.setattr(bar, "showMessage", show)
    tts.get_state = lambda: TTSState.FAILED
    try:
        bar._timer.start(5)
        deadline = time.monotonic() + 1
        while not notices.history() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert len(notices.history()) == 1
        assert notices.history()[0].title == "语音播报未成功"
        show.assert_not_called()
        bar.refresh()
        assert len(notices.history()) == 1
        tts._generation += 1
        bar.refresh()
        assert len(notices.history()) == 1  # 连续无关联播报故障合并，避免刷屏。
    finally:
        bar._timer.stop()
        bar.close()


def test_recovered_tts_still_shows_the_previous_failure(monkeypatch, notices):
    app = QApplication.instance() or QApplication([])
    tts = SimpleNamespace(get_state=lambda: TTSState.IDLE, _generation=2,
                          get_last_failure=lambda: None)
    voice = SimpleNamespace(is_mic_on=lambda: False, tts_provider=lambda: tts)
    bar = StatusBar({"voice": voice})
    bar._timer.stop()
    show = Mock()
    monkeypatch.setattr(bar, "showMessage", show)
    tts.get_last_failure = lambda: (1, "语音播报失败：合成未完成")
    try:
        bar.refresh()
        assert len(notices.history()) == 1
        assert notices.history()[0].title == "语音播报未成功"
        show.assert_not_called()
        bar.refresh()
        assert len(notices.history()) == 1
    finally:
        bar.close()
