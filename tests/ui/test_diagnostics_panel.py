from types import SimpleNamespace
import time
from PySide6.QtWidgets import QApplication

from app.ui.widgets.diagnostics_panel import DiagnosticsPanel
from app.ui.dialogs.identify_dialog import _OnlineIdentifyWorker
from app.music.shazamio_provider import ShazamRecognitionStatus


def test_panel_runs_check_and_displays_result(monkeypatch):
    app = QApplication.instance() or QApplication([])
    import app.ui.widgets.diagnostics_panel as module
    monkeypatch.setattr(module, "collect_report", lambda: {
        "ok": False, "checks": [{"component": "shazamio", "ok": False,
        "error_type": "ModuleNotFoundError", "missing_module": "pydantic"}]})
    panel = DiagnosticsPanel()
    panel._start()
    panel._start()  # repeated requests do not create another worker
    deadline = time.monotonic() + 3
    while panel._worker is not None and time.monotonic() < deadline:
        app.processEvents()
    assert panel._worker is None
    assert "缺少 pydantic" in panel.output.toPlainText()
    assert panel.run_button.isEnabled()
    panel.close()


def test_unexpected_provider_error_delivers_one_failure():
    def fail(path):
        raise RuntimeError("unexpected")
    worker = _OnlineIdentifyWorker("unused.wav", SimpleNamespace(recognize=fail))
    results = []
    worker.done.connect(results.append)
    worker.run()
    assert len(results) == 1
    assert results[0].status is ShazamRecognitionStatus.SERVICE_ERROR
    assert "请重试" in results[0].detail
