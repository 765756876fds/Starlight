"""Keep Qt's native widget list alive while PySide builds its Python wrappers."""
import gc

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication


def snapshot_widgets(app=None, *, top_level=False):
    """Materialize a GUI-thread snapshot without re-entrant automatic GC.

    Wrapper allocation can collect an unreachable dialog still present in Qt's
    native pointer list. Keep this guard synchronous and limited to conversion;
    callers receive strong references before the original GC state is restored.
    Explicit gc.collect() and unrelated widget lifetime errors are not covered.
    """
    app = app if app is not None else QApplication.instance()
    if app is None:
        return []
    if not isinstance(app, QApplication):
        raise TypeError("A QApplication is required for widget snapshots")
    if QThread.currentThread() != app.thread():
        raise RuntimeError("Widget snapshots must be taken on the GUI thread")
    enabled = gc.isenabled()
    if enabled:
        gc.disable()
    try:
        return list(app.topLevelWidgets() if top_level else app.allWidgets())
    finally:
        if enabled:
            gc.enable()
