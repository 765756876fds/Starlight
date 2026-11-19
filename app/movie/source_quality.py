"""Opt-in observed source evidence. No HTTP, persistent history or playback actions."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import statistics
import threading
from urllib.parse import urlsplit

from .playback_diagnostics import number, source_tag


def media_origin(url):
    """Anonymous origin only; queries, credentials, paths and titles are discarded."""
    try:
        value = urlsplit(str(url))
        if value.scheme not in ("http", "https") or not value.hostname:
            return None
        port = value.port or (443 if value.scheme == "https" else 80)
        return source_tag(f"{value.scheme}://{value.hostname.casefold()}:{port}")
    except ValueError:
        return None


@dataclass(frozen=True)
class QualityEvidence:
    source: str
    origin: str
    session: int
    observed_at: float
    state: str = "UNKNOWN"
    reason: str = "insufficient_evidence"
    progress_s: float = 0.
    waits: int = 0
    wait_lower_s: float = 0.
    startup_s: float | None = None
    input_bps_median: float | None = None
    advisory_eligible: bool = False


class SourceQualitySession:
    """Conservative clock-driven classifier, deliberately independent of UI.

    A source is observed stable after >=30s continuous verified advancement.
    Cache waits count only after >=8s verified advancement and outside a 5s
    transition grace. Two positive wait snapshots confirm an episode; durations
    are lower bounds between observations, not exact stall times.
    """
    def __init__(self, source, origin, session, started_at, *, measure_startup=True):
        self.source, self.origin, self.session = source, origin, session
        self.started_at = started_at
        self._previous = None
        self._epoch = None
        self._grace_until = started_at
        self._warm_s = self._progress_s = 0.
        self._wait_start = None
        self._wait_lower = self._completed_wait_lower = 0.
        self._wait_confirmed = False
        self._events = deque(maxlen=64)
        self._rates = deque(maxlen=32)
        self._wait_count = 0
        self._last_advisory = float("-inf")
        self._startup_allowed = measure_startup
        self._startup_s = None
        self._armed = False

    def dismiss(self, now):
        self._last_advisory = now

    def _transition(self, now):
        self._finish_wait()
        self._previous = None
        self._warm_s = 0.
        self._armed = False
        self._events.clear()
        self._grace_until = now + 5
        self._startup_allowed = False

    def _finish_wait(self):
        self._completed_wait_lower += self._wait_lower
        self._wait_start = None
        self._wait_lower = 0.
        self._wait_confirmed = False

    def _report(self, now, state, reason, advisory=False):
        return QualityEvidence(
            self.source, self.origin, self.session, now, state, reason,
            round(self._progress_s, 3), self._wait_count,
            round(self._completed_wait_lower + self._wait_lower, 3), self._startup_s,
            statistics.median(self._rates) if self._rates else None, advisory,
        )

    def observe(self, sample, now, epoch=0):
        if self._epoch is None:
            self._epoch = epoch
        elif epoch != self._epoch:
            self._epoch = epoch
            self._transition(now)
        pos, speed = number(sample.get("time-pos")), number(sample.get("speed"))
        essential = all(type(sample.get(k)) is bool for k in (
            "pause", "seeking", "paused-for-cache", "core-idle", "eof-reached",
        ))
        if not essential or pos is None or speed is None or not .1 <= speed <= 4:
            if self._startup_allowed and self._startup_s is None and self._previous is None:
                return self._report(now, "UNKNOWN", "initial_load_pending")
            self._transition(now)
            return self._report(now, "UNKNOWN", "evidence_gap")
        if sample["pause"] or sample["seeking"] or sample["eof-reached"]:
            self._transition(now)
            return self._report(now, "EXCLUDED", (
                "user_pause" if sample["pause"] else "seek" if sample["seeking"] else "eof"
            ))
        old = self._previous
        dt = now - old[1] if old else 0
        delta = pos - old[0]["time-pos"] if old else 0
        if old and (dt <= 0 or dt > 5):
            self._transition(now)
            return self._report(now, "UNKNOWN", "sample_gap")
        # Catch seeks shorter than the 2s poll interval and timeline discontinuities.
        if old and (delta < -.25 or delta > dt * speed + max(.75, dt * speed * .35)):
            self._transition(now)
            return self._report(now, "EXCLUDED", "position_jump")
        self._previous = sample, now
        if now < self._grace_until:
            return self._report(now, "EXCLUDED", "transition_grace")
        rate = number(sample.get("cache-speed"))
        if rate is not None and rate > 0:
            self._rates.append(rate)
        cache = number(sample.get("demuxer-cache-duration"))
        dropping = False
        if old and cache is not None and cache >= 2:
            for key in ("frame-drop-count", "decoder-frame-drop-count"):
                a, b = number(sample.get(key)), number(old[0].get(key))
                dropping |= a is not None and b is not None and a > b
        if dropping:
            self._transition(now)
            return self._report(now, "UNKNOWN", "decode_or_render_pressure")
        while self._events and now - self._events[0] > 60:
            self._events.popleft()
        if sample["paused-for-cache"]:
            if not self._armed:
                return self._report(now, "OBSERVING", "startup_or_recovery_wait")
            if self._wait_start is None:
                self._wait_start = now
            self._wait_lower = max(0., now - self._wait_start)
            if self._wait_lower >= 1 and not self._wait_confirmed:
                self._wait_confirmed = True
                self._wait_count += 1
                self._events.append(self._wait_start)
            eligible = (self._wait_lower >= 3 or len(self._events) >= 2)
            advisory = eligible and now - self._last_advisory >= 120
            if advisory:
                self._last_advisory = now
            return self._report(now, "BUFFERING" if self._wait_confirmed else "OBSERVING",
                                "continuous_cache_wait", advisory)
        if self._wait_start is not None:
            self._warm_s = 0.
        self._finish_wait()
        advancing = (old is not None and max(.01, dt * speed * .6) <= delta
                     <= dt * speed + max(.75, dt * speed * .35))
        if not sample["core-idle"] and advancing:
            self._progress_s += dt
            self._warm_s += dt
            self._armed |= self._warm_s >= 8
            if self._startup_allowed and self._startup_s is None:
                self._startup_s = round(now - self.started_at, 3)
            return self._report(now, "STABLE" if self._warm_s >= 30 and not self._events else "OBSERVING",
                                "verified_progress")
        self._warm_s = 0.
        return self._report(now, "UNKNOWN", "no_verified_progress")


class SourceQualityHistory:
    """Bounded process-local source aggregates; no content IDs or watching history.

    At most 32 source/origin pairs, four sessions each, 15 minute freshness.
    Unknown updates cannot silently become failures or empty successful evidence.
    """
    def __init__(self, limit=32, ttl=900):
        self._limit, self._ttl = limit, ttl
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def update(self, report):
        if report.state not in ("STABLE", "BUFFERING", "FAILED"):
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            key = report.source, report.origin
            sessions = self._items.pop(key, OrderedDict())
            sessions[report.session] = report
            while len(sessions) > 4:
                sessions.popitem(last=False)
            self._items[key] = sessions
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
            return True
        finally:
            self._lock.release()

    def snapshot(self, now):
        # GUI callers must never wait for the background collector.
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return tuple(r for sessions in self._items.values() for r in sessions.values()
                         if 0 <= now - r.observed_at <= self._ttl)
        finally:
            self._lock.release()


def compare_evidence(current, alternative, now, pinned=False):
    """Evidence comparison only, NOT media identity permission or a route change."""
    if pinned:
        return "KEEP_CURRENT", "user_pinned"
    if current is None or alternative is None:
        return "KEEP_CURRENT", "unknown"
    if current.origin == alternative.origin:
        return "KEEP_CURRENT", "shared_media_origin"
    if any(not 0 <= now - r.observed_at <= 900 for r in (current, alternative)):
        return "KEEP_CURRENT", "stale_evidence"
    if current.state == "BUFFERING" and alternative.state == "STABLE":
        return "ALTERNATIVE_EVIDENCE_BETTER", "verified_stability_not_peak_speed"
    return "KEEP_CURRENT", "no_reliable_quality_advantage"
