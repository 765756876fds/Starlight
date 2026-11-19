"""Windows application exit must bypass the window's normal hide-to-tray veto."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows WM_QUIT")
@pytest.mark.parametrize("stage", ["ready", "starting"])
def test_native_quit_reaches_about_to_quit_with_veto_window(tmp_path, stage):
    probe = r'''
import ctypes
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from app.ui.main_window import ApplicationQuitFilter

class VetoWindow(QWidget):
    _force_quit = False
    def closeEvent(self, event):
        event.ignore()

app = QApplication([])
window = VetoWindow()
window.show()
guard = ApplicationQuitFilter(app, window)
app.installEventFilter(guard)
if sys.argv[1] == "ready":
    guard.startup_finished()
else:
    QTimer.singleShot(300, guard.startup_finished)
notifications = []
app.aboutToQuit.connect(lambda: notifications.append(guard.ready))
thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
QTimer.singleShot(25, lambda: ctypes.windll.user32.PostThreadMessageW(thread_id, 18, 0, 0))
QTimer.singleShot(2500, lambda: app.exit(74))
code = app.exec()
assert code == 0 and guard.pending and notifications == [True], (code, guard.pending, notifications)
'''
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path), "QT_QPA_PLATFORM": "windows",
           "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    result = subprocess.run([sys.executable, "-B", "-c", probe, stage], env=env,
                            cwd=tmp_path, timeout=12, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
