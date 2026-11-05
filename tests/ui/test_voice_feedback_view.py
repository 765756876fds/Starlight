"""Voice feedback now lives in the capsule and notification center, without a popup."""
import threading
from PySide6.QtCore import QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.core import notifications as module
from app.core.notifications import NotificationCenter
from app.ui.panels.status_bar import StatusBar
from app.voice.runtime import VoiceRuntime


def test_capsule_opens_history_without_starting_microphone(monkeypatch):
    app = QApplication.instance() or QApplication([])
    center = NotificationCenter()
    monkeypatch.setattr(module, "_center", center)
    runtime = VoiceRuntime({})
    bar = StatusBar({"voice": runtime})
    bar._timer.stop()
    opened = []
    bar.notification_requested.connect(lambda: opened.append(True))
    bar.island.click()
    bar.island.click()
    assert opened == [True, True]
    assert not runtime.is_mic_on()
    assert not hasattr(bar, "_voice_feedback_dialog")
    bar._notice_stream.dispose()
    bar.close()


def test_worker_feedback_is_rendered_on_gui_thread(monkeypatch):
    app = QApplication.instance() or QApplication([])
    center = NotificationCenter()
    monkeypatch.setattr(module, "_center", center)
    calls = []
    class CheckedBar(StatusBar):
        def _refresh_island(self):
            assert QThread.currentThread() == app.thread()
            calls.append(True)
            super()._refresh_island()
    bar = CheckedBar()
    bar._timer.stop()
    thread = threading.Thread(target=lambda: center.post("技能计时已取消", status="success", source="voice", phase="done"))
    thread.start()
    thread.join(timeout=1)
    QTest.qWait(20)
    assert not thread.is_alive()
    assert len(calls) >= 2
    assert bar.island.text() == "技能计时已取消"
    bar._notice_stream.dispose()
    bar.close()


def test_missing_result_is_not_displayed_as_success(monkeypatch):
    app = QApplication.instance() or QApplication([])
    center = NotificationCenter()
    monkeypatch.setattr(module, "_center", center)
    bar = StatusBar()
    bar._timer.stop()
    center.post("操作已发送，结果尚未确认", status="warning", source="voice", phase="done")
    QTest.qWait(10)
    assert bar.island._status == "warning"
    assert "尚未确认" in bar.island.text()
    bar._notice_stream.dispose()
    bar.close()
