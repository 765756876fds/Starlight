"""One background read at a time, retaining only the newest requested read."""
import logging
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

logger = logging.getLogger(__name__)


class LatestTask(QObject):
    completed = Signal(object, object, object)  # key, value, exception
    _returned = Signal(int, object, object, object)

    def __init__(self, parent=None, *, timeout_ms=0):
        super().__init__(parent)
        self._serial = 0
        self._running = False
        self._pending = None
        self._closed = False
        self._timeout_ms = timeout_ms
        self._expired_running = False
        self._deadline = None
        self._timer = QTimer(self) if timeout_ms else None
        if self._timer is not None:
            self._timer.setSingleShot(True)
            self._timer.setTimerType(Qt.PreciseTimer)
            self._timer.timeout.connect(self._expire)
        self._returned.connect(self._finish, Qt.QueuedConnection)
        self.destroyed.connect(self.cancel)

    def submit(self, key, read, *, guarded=False):
        if self._closed:
            return
        self._serial += 1
        if self._expired_running:
            # Never spawn replacement workers for an uninterruptible old read.
            self.completed.emit(key, None, TimeoutError("previous read still finishing"))
            return
        self._pending = (self._serial, key, read, guarded)
        self._latest_key = key
        self._deadline = time.monotonic() + self._timeout_ms / 1000 if self._timeout_ms else None
        if self._timer is not None:
            self._timer.start(self._timeout_ms)  # Includes time waiting behind an old read.
            logger.info("content_read stage=submit serial=%s queued=%s", self._serial, self._running)
        if not self._running:
            self._start_pending()

    def invalidate(self):
        """Ignore old completions; does not cancel an already issued HTTP read."""
        self._serial += 1
        self._pending = None
        self._deadline = None
        if self._timer is not None:
            self._timer.stop()

    def _expire(self):
        if self._closed or self._deadline is None:
            return
        key = self._latest_key
        logger.warning("content_read stage=timeout serial=%s running=%s", self._serial, self._running)
        self.invalidate()
        self._expired_running = self._running
        self.completed.emit(key, None, TimeoutError("read deadline exceeded"))

    def cancel(self, *_):
        self._closed = True
        self.invalidate()

    def _start_pending(self):
        if not self._pending or self._closed:
            return
        serial, key, read, guarded = self._pending
        deadline = self._deadline
        self._pending = None
        self._running = True
        def current():
            return (not self._closed and serial == self._serial
                    and (deadline is None or time.monotonic() < deadline))
        current.request_serial = serial

        def work():
            value, error = None, None
            started = time.monotonic()
            try:
                if self._timeout_ms:
                    logger.info("content_read stage=started serial=%s", serial)
                value = read(current) if guarded else read()
            except Exception as exc:
                logger.warning("background_read_failed serial=%s type=%s", serial, type(exc).__name__)
                error = exc
            try:
                if self._timeout_ms:
                    logger.info("content_read stage=returned serial=%s elapsed_ms=%s", serial,
                                round((time.monotonic() - started) * 1000))
                self._returned.emit(serial, key, value, error)
            except RuntimeError:
                pass  # Parent was deleted; never access its widgets from this thread.

        threading.Thread(target=work, name="StarlightContentRead", daemon=True).start()

    @Slot(int, object, object, object)
    def _finish(self, serial, key, value, error):
        self._running = False
        self._expired_running = False
        if self._timeout_ms:
            logger.info("content_read stage=finish serial=%s current=%s closed=%s",
                        serial, serial == self._serial, self._closed)
        if not self._closed and serial == self._serial:
            if self._timer is not None:
                self._timer.stop()
            if self._deadline is not None and time.monotonic() >= self._deadline:
                error = TimeoutError("read deadline exceeded")
            self._deadline = None
            if self._timeout_ms:
                logger.info("content_read stage=emit serial=%s error_type=%s", serial,
                            type(error).__name__ if error else "none")
            self.completed.emit(key, value, error)
            if self._timeout_ms:
                logger.info("content_read stage=delivered serial=%s", serial)
        self._start_pending()
