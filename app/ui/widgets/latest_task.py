"""One background read at a time, retaining only the newest requested read."""
import logging
import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot

logger = logging.getLogger(__name__)


class LatestTask(QObject):
    completed = Signal(object, object, object)  # key, value, exception
    _returned = Signal(int, object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._serial = 0
        self._running = False
        self._pending = None
        self._closed = False
        self._returned.connect(self._finish, Qt.QueuedConnection)
        self.destroyed.connect(self.cancel)

    def submit(self, key, read):
        if self._closed:
            return
        self._serial += 1
        self._pending = (self._serial, key, read)
        if not self._running:
            self._start_pending()

    def invalidate(self):
        """Ignore old completions; does not cancel an already issued HTTP read."""
        self._serial += 1
        self._pending = None

    def cancel(self, *_):
        self._closed = True
        self.invalidate()

    def _start_pending(self):
        if not self._pending or self._closed:
            return
        serial, key, read = self._pending
        self._pending = None
        self._running = True

        def work():
            value, error = None, None
            try:
                value = read()
            except Exception as exc:
                logger.exception("background_read_failed task=%s type=%s", key, type(exc).__name__)
                error = exc
            try:
                self._returned.emit(serial, key, value, error)
            except RuntimeError:
                pass  # Parent was deleted; never access its widgets from this thread.

        threading.Thread(target=work, name="StarlightContentRead", daemon=True).start()

    @Slot(int, object, object, object)
    def _finish(self, serial, key, value, error):
        self._running = False
        if not self._closed and serial == self._serial:
            self.completed.emit(key, value, error)
        self._start_pending()
