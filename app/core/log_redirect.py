import sys
from datetime import datetime
from app.core.log_bus import get_log_bus


class TeeStream:
    def __init__(self, original=None):
        self._original = original
        self._bus = get_log_bus()
        self._buffer = ""

    def write(self, data):
        if self._original:
            try:
                self._original.write(data)
            except Exception:
                pass
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                ts = datetime.now().strftime("%H:%M:%S")
                try:
                    self._bus.sig_log.emit(f"[{ts}] {line}")
                except Exception:
                    pass
        return len(data)

    def flush(self):
        if self._original:
            try:
                self._original.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def install_log_redirect():
    sys.stdout = TeeStream(sys.stdout)
    sys.stderr = TeeStream(sys.stderr)
