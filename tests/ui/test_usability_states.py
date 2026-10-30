from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication
from app.ui.panels.status_bar import StatusBar
from app.ui.widgets.diagnostics_panel import _CheckThread


def test_voice_button_tracks_backend_changes():
    app = QApplication.instance() or QApplication([])
    voice = MagicMock()
    voice.is_mic_on.return_value = True
    bar = StatusBar({"voice": voice})
    try:
        assert bar.btn_mic.isChecked()
        voice.is_mic_on.return_value = False
        bar.refresh()
        assert not bar.btn_mic.isChecked()
        assert "等待游戏客户端" in bar.lbl_lcu.text()
    finally:
        bar._timer.stop()
        bar.close()


def test_diagnostic_exception_still_delivers_failure(monkeypatch):
    import app.ui.widgets.diagnostics_panel as module
    def fail():
        raise RuntimeError("private/token")
    monkeypatch.setattr(module, "collect_report", fail)
    worker = _CheckThread()
    results = []
    worker.result.connect(results.append)
    worker.run()
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "private" not in str(results)


def test_online_failure_restores_controls_and_gives_next_step():
    from app.ui.dialogs.identify_dialog import IdentifyDialog
    from app.music.shazamio_provider import ShazamRecognitionResult, ShazamRecognitionStatus
    dialog = SimpleNamespace(btn_start=MagicMock(), source_combo=MagicMock(),
        btn_cancel=MagicMock(), _online_cancelled=False, _busy=True,
        _cleanup_wav=MagicMock(), lbl_title=MagicMock(), lbl_status=MagicMock())
    IdentifyDialog._on_online_done(dialog, ShazamRecognitionResult(
        ShazamRecognitionStatus.DEPENDENCY_MISSING))
    dialog.btn_start.setEnabled.assert_called_once_with(True)
    dialog.source_combo.setEnabled.assert_called_once_with(True)
    assert "诊断" in dialog.lbl_status.setText.call_args.args[0]
    assert dialog._busy is False
