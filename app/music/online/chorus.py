"""Recording-bound chorus metadata. Times exposed to the player are seconds."""
from __future__ import annotations

from collections import OrderedDict
import math
import threading
import time
import re
from dataclasses import dataclass, replace
from enum import Enum


class Status(str, Enum):
    INTERVAL = "interval"
    POINT = "point"
    MISSING = "missing"
    NOT_QUERIED = "not_queried"
    FAILED = "lookup_failed"
    MISMATCH = "identity_mismatch"
    PARTIAL = "limited_range"
    UNVERIFIED = "unconfirmed"


@dataclass(frozen=True)
class PlatformChorus:
    epoch: int
    rid: str  # catalog track ID, not a KuGou media hash
    start: float
    end: float | None
    duration: float
    platform: str = "酷我音乐"
    media_id: str = ""
    origin: str = "kuwo.refrain"

    @classmethod
    def from_track(cls, track, resolved_rid, epoch):
        return resolve_chorus(track, {"kuwo_rid": resolved_rid}, epoch).marker

    def fits(self, actual_seconds):
        return (type(actual_seconds) in (int, float) and math.isfinite(actual_seconds)
                and self.start < actual_seconds and (self.end is None or self.end <= actual_seconds)
                and abs(actual_seconds - self.duration) <= 1.0)


# Compatibility for the original Kuwo slider and callers.
KuwoChorus = PlatformChorus


@dataclass(frozen=True)
class Decision:
    status: Status
    marker: PlatformChorus | None = None


def _ms(value):
    if type(value) is int and 0 <= value < 10**12:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        return int(value)
    return None


def _marker(track, epoch, media_id, origin, start_ms, end_ms, *, point=False, duration=None):
    start, end = _ms(start_ms), _ms(end_ms)
    catalog = getattr(track, "duration_seconds", None)
    if type(catalog) not in (float, int) or not math.isfinite(catalog) or catalog <= 0:
        return Decision(Status.MISSING)
    if duration is None:
        duration = catalog
    if abs(duration - catalog) > 1:
        return Decision(Status.MISMATCH)
    if start is None or start >= duration * 1000:
        return Decision(Status.MISSING)
    if not (point and end_ms is None) and (end is None or not start < end <= duration * 1000):
        return Decision(Status.MISSING)
    marker = PlatformChorus(epoch, track.track_id, start/1000, None if end is None else end/1000,
                            duration, track.source_name, media_id, origin)
    return Decision(Status.POINT if end is None else Status.INTERVAL, marker)


class ChorusLookup:
    """A bounded reader is supplied only for a permitted provider. Called by the progress worker."""

    def __init__(self, read, *, clock=time.monotonic):
        self._read = read
        self._clock = clock
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def after_start(self, track, playback, epoch):
        initial = resolve_chorus(track, playback, epoch)
        proof = playback.get("playback_metadata") or {}
        if (initial.status != Status.NOT_QUERIED or not isinstance(proof, dict)
                or (track.source_name, proof.get("branch")) not in (
                    ("酷狗音乐", "kugou.direct"), ("网易音乐", "netease.direct"))):
            return initial
        key = (track.source_name, track.track_id, proof["media_id"], track.duration_seconds)
        with self._lock:
            entry = self._cache.get(key)
            if entry and self._clock() < entry[0]:
                self._cache.move_to_end(key)
                return Decision(entry[1].status, replace(entry[1].marker, epoch=epoch))
        try:
            reply = self._read(proof["branch"], track.track_id, proof["media_id"])
        except (TimeoutError, ValueError, OSError):
            return Decision(Status.FAILED)
        if reply is None:
            return Decision(Status.FAILED)
        decision = resolve_chorus(track, playback, epoch, reply=reply)
        if decision.marker:
            with self._lock:
                self._cache[key] = (self._clock() + 300, decision)
                self._cache.move_to_end(key)
                if len(self._cache) > 64:
                    self._cache.popitem(last=False)
        return decision


def resolve_chorus(track, playback, epoch, *, reply=None):
    """A reply, when supplied, must come from the adapter for the current recording."""
    extra = getattr(track, "extra", None)
    raw = extra.get("_raw") if isinstance(extra, dict) else None
    rid = getattr(track, "track_id", None)
    if not isinstance(raw, dict) or not isinstance(rid, str) or not re.fullmatch(r"[0-9]{1,24}", rid):
        return Decision(Status.UNVERIFIED)
    raw_id = raw.get("id")
    if type(raw_id) not in (str, int) or str(raw_id) != rid:
        return Decision(Status.MISMATCH)
    if track.source_name == "酷我音乐":
        segment = raw.get("refrain")
        if not isinstance(segment, dict):
            return Decision(Status.MISSING)
        if not playback.get("kuwo_rid"):
            return Decision(Status.UNVERIFIED)
        if raw_id != rid or playback["kuwo_rid"] != rid or segment.get("rid") != rid:
            return Decision(Status.MISMATCH)
        return _marker(track, epoch, rid, "kuwo.refrain", segment.get("start_ms"), segment.get("end_ms"))

    proof = playback.get("playback_metadata")
    if not isinstance(proof, dict):
        return Decision(Status.UNVERIFIED)
    if proof.get("status") == "limited_range":
        return Decision(Status.PARTIAL)
    if proof.get("status") != "matched":
        return Decision(Status.UNVERIFIED)
    if proof.get("id") != rid:
        return Decision(Status.MISMATCH)
    media = proof.get("media_id")
    if not isinstance(media, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media):
        return Decision(Status.MISMATCH)
    branch = proof.get("branch")
    if track.source_name == "开心汽水" and branch == "qishui.play":
        duration = _ms(proof.get("duration_ms"))
        if not duration:
            return Decision(Status.MISSING)
        return _marker(track, epoch, media, branch, proof.get("start_ms"), proof.get("end_ms"),
                       point=True, duration=duration/1000)

    if (track.source_name, branch) not in (("网易音乐", "netease.direct"), ("酷狗音乐", "kugou.direct")):
        return Decision(Status.UNVERIFIED)
    if not re.fullmatch(r"[0-9a-fA-F]{32}", media):
        return Decision(Status.MISMATCH)
    if reply is None:
        return Decision(Status.NOT_QUERIED)
    if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
        return Decision(Status.FAILED)
    if branch == "netease.direct":
        if reply.get("code") != 200:
            return Decision(Status.FAILED)
        rows = [r for r in reply["data"] if isinstance(r, dict) and type(r.get("id")) is int
                and str(r["id"]) == rid]
        keys = ("startTime", "endTime")
    else:
        if reply.get("status") != 1:
            return Decision(Status.FAILED)
        rows = [r for r in reply["data"] if isinstance(r, dict) and isinstance(r.get("hash"), str)
                and r["hash"].lower() == media.lower()]
        keys = ("start_time", "end_time")
    if not reply["data"]:
        return Decision(Status.MISSING)
    if len(rows) != 1:
        return Decision(Status.MISMATCH)
    return _marker(track, epoch, media.lower(), branch + ".chorus", *(rows[0].get(k) for k in keys))
