import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

_app = QApplication.instance() or QApplication([])


def test_second_start_notifies_existing_instance_without_business_init(tmp_path):
    from app.ui.single_instance import SingleInstance

    primary = SingleInstance("test_starlight", lambda: None, data_dir=str(tmp_path))
    assert primary.acquire_or_notify() == "primary"
    assert primary._lock.staleLockTime() == 0
    try:
        secondary = SingleInstance("test_starlight", lambda: None, data_dir=str(tmp_path))
        assert secondary.acquire_or_notify(timeout_ms=500) == "secondary"
        assert secondary._owns_lock is False
    finally:
        primary.close()


def test_wake_message_reaches_primary(tmp_path):
    from app.ui.single_instance import SingleInstance

    called = MagicMock()
    instance = SingleInstance("test_starlight_wake", called, data_dir=str(tmp_path))
    assert instance.acquire_or_notify() == "primary"
    sender = None
    try:
        sender = SingleInstance("test_starlight_wake", lambda: None, data_dir=str(tmp_path))
        assert sender.acquire_or_notify(timeout_ms=500) == "secondary"
        for _ in range(10):
            QApplication.processEvents()
            if called.called:
                break
        called.assert_called_once_with()
    finally:
        if sender is not None:
            sender.close()
        instance.close()


def test_wake_main_window_restores_and_activates():
    from main import Application

    app = Application.__new__(Application)
    window = MagicMock()
    window.isMinimized.return_value = True
    window.isVisible.return_value = True
    window.windowState.return_value = Qt.WindowNoState
    app._main_window = window
    app._wake_main_window()
    window.showNormal.assert_called_once_with()
    window.raise_.assert_called_once_with()
    window.activateWindow.assert_called_once_with()
