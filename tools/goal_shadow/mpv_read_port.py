"""Independent Windows MPV query connection; only a get_property whitelist.

The caller supplies an exact IPC endpoint; no discovery, launch or player stop.
This connection never shares the production MpvController reply stream/lock.
"""
import ctypes
import json
import os
import time

ALLOWED = frozenset(("idle-active", "pause", "path", "time-pos"))


class MpvReadPort:
    def __init__(self, endpoint):
        if os.name != "nt" or not str(endpoint).startswith("\\\\.\\pipe\\"):
            raise ValueError("explicit_windows_named_pipe_required")
        self._endpoint = endpoint
        self._stream = None
        self._buffer = b""
        self._sequence = 0

    def _connect(self, deadline):
        if self._stream is not None:
            return
        milliseconds = max(1, min(50, int((deadline - time.monotonic()) * 1000)))
        if not ctypes.windll.kernel32.WaitNamedPipeW(str(self._endpoint), milliseconds):
            raise TimeoutError("read_port_connect_unavailable")
        self._stream = open(self._endpoint, "r+b", buffering=0)

    def get_property(self, name, deadline):
        if name not in ALLOWED:
            raise ValueError("property_not_allowlisted")
        if time.monotonic() >= deadline:
            raise TimeoutError("read_port_deadline")
        self._connect(deadline)
        import msvcrt
        self._sequence += 1
        request = self._sequence
        self._stream.write(json.dumps({"command": ["get_property", name], "request_id": request}).encode() + b"\n")
        while time.monotonic() < deadline:
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                row = json.loads(line)
                if row.get("request_id") == request:
                    if row.get("error") != "success":
                        return None
                    return row.get("data")
            available = ctypes.c_ulong(0)
            handle = msvcrt.get_osfhandle(self._stream.fileno())
            ok = ctypes.windll.kernel32.PeekNamedPipe(ctypes.c_void_p(handle), None, 0, None,
                                                    ctypes.byref(available), None)
            if not ok:
                raise ConnectionError("read_port_closed")
            if available.value:
                self._buffer += self._stream.read(min(available.value, 32768))
                if len(self._buffer) > 65536:
                    raise ValueError("read_port_response_too_large")
            else:
                time.sleep(.002)
        raise TimeoutError("read_port_reply_deadline")

    def sample(self, deadline):
        props = {key: self.get_property(key, deadline) for key in ("idle-active", "path", "pause", "time-pos")}
        if props["pause"] is False and props["idle-active"] is False:
            movement_deadline = min(deadline, time.monotonic()+.15)
            while time.monotonic() < movement_deadline:
                time.sleep(min(.01, max(0, movement_deadline-time.monotonic())))
                after = self.get_property("time-pos", deadline)
                props["time-pos-after"] = after
                if type(after) in (int, float) and type(props["time-pos"]) in (int, float) and after > props["time-pos"]:
                    break
        if (props["path"] != self.get_property("path", deadline) or
                props["pause"] != self.get_property("pause", deadline) or
                props["idle-active"] != self.get_property("idle-active", deadline)):
            return {}
        return props

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
