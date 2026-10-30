"""Actual MusicPanel signal/slot speed feedback with an isolated MPV transport."""
from unittest.mock import MagicMock
import time

import pytest
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from app.music.controller import MusicController
from app.music.mpv import MpvController
from app.ui.panels.music_panel import MusicPanel


class SpeedPanel(MusicPanel):
    """Keep the production signal and methods; avoid unrelated panel services."""
    def __init__(self, controller):
        QWidget.__init__(self)
        self._music = controller
        self._speed_steps = list(MusicController.SPEED_STEPS)
        self._speed_idx = 2
        self.btn_speed = QPushButton("倍速 1x", self)
        self.lbl_status = QLabel("", self)
        self._speed_result.connect(self._after_speed)


@pytest.fixture(scope="module")
def gui():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("response,error,expected,word", [
    ({"error": "success"}, None, True, ""),
    ({"error": "property unavailable"}, None, False, "失败"),
    (None, None, None, "无法确认"),
    (None, TimeoutError("IPC timeout"), None, "超时"),
    (None, RuntimeError("IPC disconnected"), False, "失败"),
])
def test_speed_ui_signal_tracks_confirmed_failed_and_unknown_results(gui, response, error, expected, word):
    player = MpvController.__new__(MpvController)
    player._send_command = MagicMock(return_value=response, side_effect=error)
    controller = MusicController.__new__(MusicController)
    controller._mpv_available = True
    controller.mpv = player
    controller._speed = 1.0
    controller.state_store = MagicMock()
    panel = SpeedPanel(controller)
    spy = QSignalSpy(panel._speed_result)
    try:
        panel._apply_speed(1.5, 4)
        # QSignalSpy.wait holds the GIL in this PySide6 build, starving the
        # Python worker. Pump Qt events and yield Python with the same 3s bound.
        deadline = time.monotonic() + 3.0
        while not spy.count() and time.monotonic() < deadline:
            gui.processEvents()
            time.sleep(0.001)
        assert spy.count(), "speed worker did not deliver its result"
        gui.processEvents()
        assert spy.count() == 1
        assert spy.at(0)[0] is expected
        player._send_command.assert_called_once_with("set_property", ["speed", 1.5])
        if expected is True:
            assert panel._speed_idx == 4
            assert panel.btn_speed.text() == "倍速 1.5x"
            assert controller.get_speed() == 1.5
            controller.state_store.set.assert_called_once()
        else:
            assert panel._speed_idx == 2
            assert panel.btn_speed.text() == "倍速 1x"
            assert word in panel.lbl_status.text()
            assert controller.get_speed() == 1.0
            controller.state_store.set.assert_not_called()
    finally:
        panel.close()
        panel.deleteLater()
        gui.processEvents()
