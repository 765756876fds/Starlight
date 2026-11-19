"""Bounded, read-only movie IPC diagnostics. Never logs media paths or URLs."""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import threading
import time

_SOURCE_SALT = os.urandom(16)


def source_tag(value):
    """Per-process anonymous source correlation; never a URL/title in the log."""
    if isinstance(value, str) and "://" in value:
        from urllib.parse import urlsplit
        try:
            value = urlsplit(value).hostname
        except ValueError:
            value = None
    return (hashlib.blake2s(value.encode("utf-8"), key=_SOURCE_SALT, digest_size=6).hexdigest()
            if isinstance(value, str) and value else "unknown")


PROPERTIES = (
    "paused-for-cache", "demuxer-cache-duration", "cache-speed",
    "demuxer-cache-state", "frame-drop-count", "decoder-frame-drop-count",
    "hwdec-current", "time-pos", "pause", "seeking", "core-idle", "eof-reached", "speed", "duration", "video-params", "demuxer-cache-idle", "idle-active",
)
STATE_NUMBERS = ("fw-bytes", "raw-input-rate", "cache-end", "reader-pts")


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def sanitize(values):
    """Missing properties stay unknown; discard arbitrary strings/nested metadata."""
    result = {}
    for key in PROPERTIES:
        value = values.get(key)
        if key == "demuxer-cache-state":
            state = value if isinstance(value, dict) else {}
            result[key] = {k: number(state[k]) for k in STATE_NUMBERS if k in state}
        elif key == "video-params":
            result[key] = {k: number(value.get(k)) for k in ("w", "h")} if isinstance(value, dict) else {}
        elif key == "hwdec-current":
            result[key] = value if value in (
                "no", "d3d11va", "d3d11va-copy", "dxva2", "dxva2-copy",
                "nvdec", "nvdec-copy", "cuda", "vaapi", "videotoolbox",
                "vulkan", "vulkan-copy",
            ) else None
        elif key in ("pause", "seeking", "core-idle", "paused-for-cache", "eof-reached", "demuxer-cache-idle", "idle-active"):
            result[key] = value if type(value) is bool else None
        else:
            result[key] = number(value)
    return result


def read_sample(pipe_name, timeout=.3, *, expected_path=None, allow_idle=False):
    """Separate Windows IPC client: one batch, total deadline, bounded response.

    Does not acquire MpvController's shared command lock. A missing/busy/broken
    pipe or partial response yields unknown, never a synthetic zero measurement.
    Only get_property commands are permitted here.
    """
    if os.name != "nt":
        return {}
    import msvcrt
    from ctypes import wintypes

    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    peek.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    peek.restype = wintypes.BOOL
    keys = ("_before", *PROPERTIES, "_after")
    deadline = time.monotonic() + min(max(timeout, .01), .5)
    try:
        with open(pipe_name, "r+b", buffering=0) as pipe:
            packet = b"".join((json.dumps({
                "command": ["get_property", "path" if k.startswith("_") else k],
                "request_id": i,
            }) + "\n").encode() for i, k in enumerate(keys, 1))
            pipe.write(packet)
            handle = msvcrt.get_osfhandle(pipe.fileno())
            pending, found, total = b"", {}, 0
            while time.monotonic() < deadline and len(found) < len(keys):
                available = wintypes.DWORD()
                if not peek(handle, None, 0, None, ctypes.byref(available), None):
                    return {}
                if not available.value:
                    time.sleep(.005)
                    continue
                chunk = pipe.read(min(available.value, 65536 - total))
                if not chunk:
                    return {}
                total += len(chunk)
                if total >= 65536:
                    return {}
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    request_id = message.get("request_id")
                    if type(request_id) is int and 1 <= request_id <= len(keys):
                        found[keys[request_id - 1]] = (
                            message.get("data") if message.get("error") == "success" else None
                        )
            idle = (allow_idle and found.get("idle-active") is True
                    and found.get("_before") is None and found.get("_after") is None)
            if (len(found) != len(keys) or (not found.get("_before") and not idle)
                    or found["_before"] != found["_after"]
                    or (expected_path is not None
                        and found["_after"] != expected_path and not (expected_path == "" and idle))):
                return {}
            return sanitize(found)
    except (OSError, ValueError, TypeError):
        # Deliberately do not log exceptions containing pipe/media payloads.
        return {}


class PlaybackSummary:
    """Evidence categories, not an attribution of CDN/network/decode root cause."""
    def __init__(self, now):
        self.start = now
        self.previous = None
        self.first_progress = None
        self.buffer_events = 0
        self.buffer_seconds = 0.
        self.rebuffer_seconds = 0.
        self.samples = 0

    def add(self, sample, now):
        if not sample:
            return {"status": "unavailable"}
        self.samples += 1
        previous = self.previous
        position = sample.get("time-pos")
        delta = None
        if previous:
            old, old_time = previous
            if number(position) is not None and number(old.get("time-pos")) is not None:
                delta = position - old["time-pos"]
            if old.get("paused-for-cache") is True and old.get("pause") is False:
                duration = min(max(now - old_time, 0), 5)
                self.buffer_seconds += duration
                if self.first_progress is not None:
                    self.rebuffer_seconds += duration
        if (delta is not None and delta > .01 and delta < 5
                and sample.get("seeking") is False and self.first_progress is None):
            self.first_progress = now - self.start
        waiting = (sample.get("paused-for-cache") is True and sample.get("pause") is False
                   and sample.get("seeking") is False)
        if waiting and self.first_progress is not None and (
            not previous or previous[0].get("paused-for-cache") is not True
        ):
            self.buffer_events += 1
        cache = number(sample.get("demuxer-cache-duration"))
        drops = 0
        if previous:
            for key in ("frame-drop-count", "decoder-frame-drop-count"):
                a, b = number(sample.get(key)), number(previous[0].get(key))
                if a is not None and b is not None:
                    drops += max(a - b, 0)
        if sample.get("pause") is True:
            status = "user_paused"
        elif sample.get("seeking") is True:
            status = "seeking"
        elif waiting:
            status = "cache_wait" if self.first_progress is not None else "initial_cache_wait"
        elif cache is not None and cache >= 2 and drops:
            status = "decode_or_render_pressure"
        elif delta is not None and .01 < delta < 5:
            status = "progressing"
        else:
            status = "unknown"
        self.previous = sample, now
        return {
            "status": status, "samples": self.samples,
            "first_progress_s": self.first_progress,
            "rebuffer_events": self.buffer_events,
            "cache_wait_s": round(self.buffer_seconds, 3),
            "rebuffer_s": round(self.rebuffer_seconds, 3),
            **sanitize(sample),
        }


class MoviePlaybackMonitor:
    """One opt-in worker: M1 logs <=120s; optional quality observations <=4h.

    Replacing a job never starts another worker. Ownership is checked before and
    after IPC; old results cannot be logged as belonging to a new movie.
    """
    def __init__(self, emit, reader=read_sample, interval=2., duration=120.):
        self._emit, self._reader = emit, reader
        self._interval, self._duration = interval, duration
        self._condition = threading.Condition()
        self._job = None
        self._closed = False
        self._thread = None
        self._quality_epoch = 0
        self._quality_failed = None

    def start(self, pipe, generation, current, mode, source=None, *, on_sample=None,
              duration=None):
        with self._condition:
            if self._closed:
                return
            self._job = (pipe, generation, current,
                         mode if mode in ("local", "cms", "external") else "unknown",
                         source_tag(source), on_sample,
                         self._duration if duration is None else min(duration, 14400))
            self._quality_epoch += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="movie-diagnostics", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def interrupt(self, *, suspend=False):
        """Cheap controller-owned transition marker; no IPC or UI callbacks."""
        with self._condition:
            self._quality_epoch += 1
            if suspend:
                self._job = None
                self._condition.notify_all()

    def quality_current(self, epoch):
        if not self._condition.acquire(blocking=False):
            return False
        try:
            return (not self._closed and self._job is not None
                    and self._quality_failed is not self._job and self._quality_epoch == epoch)
        finally:
            self._condition.release()

    def close(self):
        with self._condition:
            self._closed = True
            self._job = None
            self._condition.notify_all()

    def _run(self):
        job = None
        while True:
            with self._condition:
                if self._closed:
                    return
                if self._job is None:
                    self._condition.wait()
                    continue
                if job is not self._job:
                    job = self._job
                    summary = PlaybackSummary(time.monotonic())
                    last_log, last_status = 0., None
            pipe, generation, current, mode, source, on_sample, duration = job
            try:
                with self._condition:
                    epoch = self._quality_epoch
                sample = self._reader(pipe) if current() else {}
                now = time.monotonic()
                with self._condition:
                    if self._closed:
                        return
                    if self._job is not job:
                        continue
                    if not current() or now - summary.start >= duration:
                        self._job = None
                        continue
                    result = summary.add(sample, now)
                    if (now - summary.start < self._duration
                            and (result["status"] != last_status or now - last_log >= 30)):
                        payload = {"generation": generation, "mode": mode,
                                   "source_tag": source, **result}
                        last_log, last_status = now, result["status"]
                    else:
                        payload = None
                if payload is not None and current():
                    self._emit(payload)
                if on_sample is not None and current():
                    with self._condition:
                        fresh = (self._job is job and not self._closed
                                 and epoch == self._quality_epoch and self._quality_failed is not job)
                    if fresh:
                        try:
                            on_sample(sample, now, epoch)
                        except Exception:
                            self._quality_failed = job
                with self._condition:
                    if self._closed:
                        return
                    if self._job is not job:
                        continue
                    self._condition.wait(self._interval)
            except Exception:
                # Observer failure must not affect the player or trigger recovery.
                with self._condition:
                    if self._job is job:
                        self._job = None
