"""Own exactly one isolated loopback llama-server; measure real Windows resources."""
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import socket
import secrets
import subprocess
import threading
import time
import urllib.request


class MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
        (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
        "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class CounterValue(ctypes.Structure):
    _fields_ = [("status", w.DWORD), ("value", ctypes.c_double)]


class CounterItem(ctypes.Structure):
    _fields_ = [("name", w.LPWSTR), ("value", CounterValue)]


class Resources:
    def __init__(self, pid):
        self.pid, self.samples = pid, []
        self.stop_event = threading.Event()
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.restype = w.HANDLE
        self.handle = self.kernel.OpenProcess(0x410, False, pid)
        if not self.handle:
            raise OSError("cannot measure owned process")
        self.pdh = ctypes.WinDLL("pdh")
        self.query, self.counters = w.HANDLE(), {}
        if not self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)):
            for name in ("Dedicated Usage", "Shared Usage"):
                counter = w.HANDLE()
                status = self.pdh.PdhAddEnglishCounterW(self.query,
                    f"\\GPU Process Memory(*)\\{name}", 0, ctypes.byref(counter))
                if not status:
                    self.counters[name] = counter
        self.worker = threading.Thread(target=self._sample, daemon=True)
        self.worker.start()

    def read(self):
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(self.handle,
                ctypes.byref(counters), counters.cb):
            return None
        row = {"at": time.monotonic(), "working_set": counters.WorkingSetSize,
               "peak_working_set": counters.PeakWorkingSetSize, "private_bytes": counters.PrivateUsage}
        created, ended, kernel, user = (w.FILETIME() for _ in range(4))
        if self.kernel.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(ended),
                                      ctypes.byref(kernel), ctypes.byref(user)):
            row["cpu_seconds"] = sum((x.dwHighDateTime << 32) + x.dwLowDateTime for x in (kernel, user)) / 1e7
        if self.query and self.counters:
            self.pdh.PdhCollectQueryData(self.query)
            for name, counter in self.counters.items():
                size, count = w.DWORD(), w.DWORD()
                self.pdh.PdhGetFormattedCounterArrayW(counter, 0x200, ctypes.byref(size), ctypes.byref(count), None)
                if size.value:
                    buffer = ctypes.create_string_buffer(size.value)
                    status = self.pdh.PdhGetFormattedCounterArrayW(counter, 0x200,
                        ctypes.byref(size), ctypes.byref(count), buffer)
                    if not status:
                        items = ctypes.cast(buffer, ctypes.POINTER(CounterItem))
                        row[name] = sum(items[i].value.value for i in range(count.value)
                            if items[i].name.startswith(f"pid_{self.pid}_") and items[i].value.status in (0, 1))
        return row

    def _sample(self):
        while not self.stop_event.is_set():
            row = self.read()
            if row:
                self.samples.append(row)
            self.stop_event.wait(.25)

    def close(self):
        self.stop_event.set()
        self.worker.join(2)
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
        self.kernel.CloseHandle(self.handle)


class Server:
    def __init__(self, binary, model, output, *, embedding=False, gpu=True):
        self.binary, self.model, self.output = Path(binary), Path(model), Path(output)
        self.embedding, self.gpu = embedding, gpu
        self.api_key = secrets.token_urlsafe(24)
        self.process = self.resources = None
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, data=None, timeout=30):
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                        headers={"Content-Type": "application/json",
                                                 "Authorization": "Bearer " + self.api_key})
        with self.http.open(request, timeout=timeout) as response:
            return json.load(response)

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        args = [str(self.binary), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                "-c", "4096", "-np", "1", "-t", "4", "-tb", "4", "-b", "512", "-ub", "128",
                "-ngl", "99" if self.gpu else "0", "--offline", "--no-warmup", "--cache-ram", "0",
                "--api-key", self.api_key]
        if self.embedding:
            args += ["--embedding", "--pooling", "last", "--embd-normalize", "2"]
        else:
            args += ["--jinja", "--reasoning", "off", "--no-cache-prompt"]
        self.log = (self.output / "server.log").open("wb")
        self.started = time.monotonic()
        self.process = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        recorded = ["<ephemeral-local-key>" if value == self.api_key else value for value in args]
        (self.output / "launch.json").write_text(json.dumps({"args": recorded, "pid": self.process.pid,
            "port": self.port}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self.resources = Resources(self.process.pid)
            deadline = self.started + 90
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"model loader exit {self.process.returncode}; see server.log")
                try:
                    if self.request("/health", timeout=1).get("status") == "ok":
                        self.load_seconds = time.monotonic() - self.started
                        return self
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            raise TimeoutError("90 second model-load budget")
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def __exit__(self, *exc):
        cleanup_error = None
        try:
            if self.resources:
                self.resources.close()
                (self.output / "resources.json").write_text(json.dumps(self.resources.samples), encoding="utf-8")
        except BaseException as error:
            cleanup_error = error
        finally:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.log.close()
        with socket.socket() as sock:
            sock.settimeout(1)
            released = sock.connect_ex(("127.0.0.1", self.port)) != 0
        (self.output / "cleanup.json").write_text(json.dumps({"pid": self.process.pid,
            "exit_code": self.process.returncode, "port_released": released,
            "sampler_stopped": self.resources is None or not self.resources.worker.is_alive(),
            "resource_error": type(cleanup_error).__name__ if cleanup_error else None}), encoding="utf-8")
        if not released:
            raise RuntimeError("owned experimental server port did not close")
        if cleanup_error and not any(exc):
            raise cleanup_error
