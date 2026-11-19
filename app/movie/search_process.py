"""Two owned, killable HTTP children; release slots only after process exit."""
import json
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal


class SearchProcessPool(QObject):
    returned = Signal(object, object)
    available = Signal()
    MAX_ACTIVE = 2
    MAX_OUTPUT = 4 * 1024 * 1024

    def __init__(self, parent=None, command=None):
        super().__init__(parent)
        executable = Path(sys.executable)
        if sys.platform == "win32" and executable.with_name("pythonw.exe").is_file():
            executable = executable.with_name("pythonw.exe")
        self.command = command or [str(executable), "-B", "-u",
                                   str(Path(__file__).with_name("search_worker.py"))]
        self.active = {}
        self.closed = False
        self.peak = 0

    def start(self, token, payload, timeout_ms):
        if self.closed or len(self.active) >= self.MAX_ACTIVE or token in self.active:
            return False
        process = QProcess(self)
        timer = QTimer(process)
        timer.setSingleShot(True)
        state = dict(process=process, timer=timer, data=bytearray(), cancelled=False,
                     switch_probe=payload.get("mode") == "verify_switch", failure=None, token=token, started=time.monotonic())
        self.active[token] = state
        self.peak = max(self.peak, len(self.active))
        process.readyReadStandardOutput.connect(lambda: self._read(token))
        process.readyReadStandardError.connect(lambda: process.readAllStandardError())
        process.finished.connect(lambda *_: self._finish(token))
        process.errorOccurred.connect(lambda e: self._error(token, e))
        process.started.connect(lambda: self._send(token, payload))
        timer.timeout.connect(lambda: self._kill(token, "Timeout"))
        process.setProgram(self.command[0])
        process.setArguments(self.command[1:])
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        timer.start(max(1, timeout_ms))  # Includes Python startup, DNS and response parsing.
        process.start()
        return True

    def _send(self, token, payload):
        state = self.active.get(token)
        if state and not state["cancelled"] and not state["failure"]:
            state["process"].write(json.dumps(payload, ensure_ascii=False).encode("utf8"))
            state["process"].closeWriteChannel()
        elif state:
            state["process"].kill()

    def _read(self, token):
        state = self.active.get(token)
        if state:
            chunk = bytes(state["process"].readAllStandardOutput())
            remaining = self.MAX_OUTPUT + 1 - len(state["data"])
            state["data"].extend(chunk[:max(0, remaining)])
            if len(state["data"]) > self.MAX_OUTPUT:
                self._kill(token, "ResponseTooLarge")

    def _kill(self, token, reason):
        state = self.active.get(token)
        if state:
            state["failure"] = reason
            state["process"].kill()

    def cancel(self, predicate=lambda _: True):
        for token, state in tuple(self.active.items()):
            if predicate(token):
                state["cancelled"] = True
                state["timer"].stop()
                state["process"].kill()

    def _error(self, token, error):
        if error == QProcess.FailedToStart:
            state = self.active.get(token)
            if state:
                state["failure"] = "FailedToStart"
                self._finish(token)

    def _finish(self, token):
        state = self.active.get(token)
        if not state or state["process"].state() != QProcess.NotRunning:
            return
        self._read(token)
        self.active.pop(token)
        state["timer"].stop()
        result = {"status": "failed", "error": state["failure"] or "WorkerExit", "items": []}
        if not state["failure"] and state["process"].exitCode() == 0:
            try:
                value = json.loads(state["data"])
                statuses = ("ok", "empty", "failed", "restart_available") if state["switch_probe"] else ("ok", "empty", "failed")
                if (isinstance(value, dict) and value.get("status") in statuses
                        and isinstance(value.get("items"), list)
                        and all(isinstance(i, dict) for i in value["items"])):
                    result = value
            except (ValueError, UnicodeError):
                result["error"] = "WorkerProtocol"
        state["process"].deleteLater()
        result["total_elapsed_ms"] = round((time.monotonic()-state["started"])*1000)
        if not state["cancelled"] and not self.closed:
            self.returned.emit(token, result)
        self.available.emit()

    def close(self):
        self.closed = True
        self.cancel()

    def shutdown(self):
        """App-exit-only bounded reap; interactive cancellation never waits."""
        self.close()
        for state in tuple(self.active.values()):
            state["process"].waitForFinished(200)
