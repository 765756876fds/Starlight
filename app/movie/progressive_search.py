"""Progressive manual search and volatile search health, sharing two process slots."""
from collections import Counter, OrderedDict, deque
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Signal

from app.core.logger import get_logger
from .search_process import SearchProcessPool

logger = get_logger()


def source_identity(url):
    try:
        p = urlsplit(str(url).strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            return None
        user, separator, host = p.netloc.rpartition("@")
        authority = user + separator + host.lower() if separator else p.netloc.lower()
        return hashlib.sha256(urlunsplit((p.scheme.lower(), authority, p.path,
                                          p.query, "")).encode()).hexdigest()
    except ValueError:
        return None


def work_identity(item):
    """Only explicit external IDs plus corroborating title/year/type may cap cards.

    This groups candidates, never asserts the cuts/episodes are interchangeable.
    Missing or conflicting identity stays separate; no progress mapping is allowed.
    """
    title = str(item.get("vod_name") or "").strip().casefold()
    year = str(item.get("vod_year") or "")
    kind = str(item.get("type_name") or item.get("vod_type") or "").strip()
    if not title or not re.fullmatch(r"\d{4}", year) or not kind:
        return None
    for field, pattern in (("imdb_id", r"tt\d{7,10}"), ("tmdb_id", r"[1-9]\d{0,9}"),
                           ("douban_id", r"[1-9]\d{0,9}")):
        identity = str(item.get(field) or "")
        if re.fullmatch(pattern, identity):
            return field, identity, title, year, kind
    return None


class SearchHealth:
    """Search protocol availability, not movie throughput or playback quality."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.records = OrderedDict()

    def observe(self, identity, result):
        if not identity:
            return
        record = self.records.setdefault(identity, deque(maxlen=8))
        record.append((self.clock(), result.get("status") in ("ok", "empty"),
                       max(0, result.get("total_elapsed_ms", result.get("elapsed_ms", 10000)))))
        self.records.move_to_end(identity)
        while len(self.records) > 128:
            self.records.popitem(last=False)

    def rank(self, identity):
        now = self.clock()
        recent = [x for x in self.records.get(identity, ()) if 0 <= now-x[0] < 600]
        if not recent:
            return 1, 0., 0.  # Unknown isn't a failure.
        failures = sum(not x[1] for x in recent)/len(recent)
        cool = not recent[-1][1] and now-recent[-1][0] < 60
        good = [x[2] for x in recent if x[1]]
        return (3 if cool else 2 if not good else 0), failures, sum(good)/len(good) if good else 0.


class ProgressiveMovieSearch(QObject):
    batch = Signal(int, object)
    completed = Signal(int, object)
    progress = Signal(int, object)
    FIRST_BATCH_MS = 30000
    BUDGET_MS = 120000  # Absolute cap, including the supplementary phase.
    REAP_MARGIN_MS = 2000
    SOURCE_MS = 10000
    PROBE_MS = 6000
    PROBE_INTERVAL_MS = 20*60*1000
    MAX_CANDIDATES = 2000

    def __init__(self, parent=None, pool=None, clock=time.monotonic):
        super().__init__(parent)
        self.pool = pool or SearchProcessPool(self)
        self.pool.returned.connect(self._returned)
        self.pool.available.connect(self._pump, Qt.QueuedConnection)
        self.clock = clock
        self.health = SearchHealth(clock)
        self.playback_rank = lambda _: 0
        self.serial = 0
        self.request = None
        self.manager = None
        self.idle = lambda: False
        self._probe_queue = deque()
        self._probe_stamp = None
        self._token = 0
        self._jobs = {}
        self.alternatives = []
        self._work_candidates = {}
        self.deadline_timer = QTimer(self)
        self.deadline_timer.setSingleShot(True)
        self.deadline_timer.timeout.connect(self._expire)
        self.phase_timer = QTimer(self)
        self.phase_timer.setSingleShot(True)
        self.phase_timer.timeout.connect(self._supplement)
        self.probe_timer = QTimer(self)
        self.probe_timer.setSingleShot(True)
        self.probe_timer.timeout.connect(self.probe)
        self.closed = False

    def configure(self, manager, idle):
        self.manager, self.idle = manager, idle
        self.probe_timer.start(8000)  # Window construction never waits for a request.

    def _sources(self):
        sources, seen = [], set()
        for row in self.manager.get_enabled_sources() if self.manager else ():
            identity = source_identity(row.get("url"))
            if identity and identity not in seen:
                seen.add(identity)
                sources.append(dict(row, identity=identity))
        return sources

    def _stamp(self):
        return (id(self.manager), self.manager.get_search_revision() if self.manager else None,
                tuple((s["identity"], s.get("name")) for s in self._sources()))

    def _ordered(self, sources):
        active = self.manager.get_active_name()
        explicit = getattr(self.manager, "get_explicit_search_source", lambda: None)()
        evidence = any(self.health.rank(s["identity"])[0] == 0 for s in sources)
        def rank(source):
            identity = source["identity"]
            tier, failures, latency = self.health.rank(identity) if evidence else (1, 0., 0.)
            default = 0 if source["name"] == active else 1 if source["name"] == "极速资源" else 2
            # Native playback evidence only breaks ties within comparable search availability.
            return (source["name"] != explicit, tier, failures, self.playback_rank(identity), latency, default)

        return sorted(sources, key=rank)

    def start(self, keyword):
        self.invalidate()
        if self.closed:
            return self.serial
        self._probe_queue.clear()
        self.probe_timer.stop()
        sources = self._ordered(self._sources())
        budget = min(self.BUDGET_MS, max(self.FIRST_BATCH_MS,
                     ((len(sources) + self.pool.MAX_ACTIVE - 1) // self.pool.MAX_ACTIVE)
                     * self.SOURCE_MS + self.REAP_MARGIN_MS))
        self.request = dict(serial=self.serial, keyword=keyword, stamp=self._stamp(),
                            deadline=self.clock()+budget/1000, budget_ms=budget, queue=deque(sources),
                            total=len(sources), attempted=0, timed_out=0, done=0, failed=0, empty=0, shown=0,
                            seen=set(), groups=Counter(), limited=False)
        self.alternatives = []
        self._work_candidates.clear()
        self.deadline_timer.start(budget)
        if budget > self.FIRST_BATCH_MS:
            self.phase_timer.start(self.FIRST_BATCH_MS)
        QTimer.singleShot(0, self._pump)  # Let the UI retain the returned generation first.
        return self.serial

    def invalidate(self):
        self.serial += 1
        self.request = None
        self.deadline_timer.stop()
        self.phase_timer.stop()
        self.pool.cancel()  # Count old processes until finished, even after kill().

    def _pump(self):
        self._jobs = {k: v for k, v in self._jobs.items() if k in self.pool.active}
        if self.closed:
            return
        req = self.request
        if req is not None:
            if req["stamp"] != self._stamp():
                self._settle("changed")
                return
            if self.clock() >= req["deadline"]:
                self._expire()
                return
            while req["queue"] and len(self.pool.active) < self.pool.MAX_ACTIVE:
                source = req["queue"].popleft()
                budget = min(self.SOURCE_MS, max(1, int((req["deadline"]-self.clock())*1000)))
                self._launch(source, "search", req["serial"], budget, req["keyword"], req["stamp"])
            live = any(j["mode"] == "search" and j["serial"] == req["serial"]
                       for token, j in self._jobs.items() if token in self.pool.active)
            if not req["queue"] and not live:
                self._settle("complete")
        elif self._probe_queue and not self.pool.active:
            if self._probe_stamp != self._stamp() or not self.idle():
                self._probe_queue.clear()
                self.probe_timer.start(self.PROBE_INTERVAL_MS)
                return
            self._launch(self._probe_queue.popleft(), "probe", self.serial,
                         self.PROBE_MS, "", self._probe_stamp)

    def _launch(self, source, mode, serial, budget, keyword, stamp):
        self._token += 1
        token = self._token
        self._jobs[token] = dict(source=source, mode=mode, serial=serial, stamp=stamp)
        if not self.pool.start(token, dict(url=source["url"], keyword=keyword, mode=mode,
                                           budget=budget/1000), budget):
            self._jobs.pop(token, None)
        elif mode == "search" and self.request and self.request["serial"] == serial:
            self.request["attempted"] += 1

    def _returned(self, token, result):
        job = self._jobs.pop(token, None)
        if not job or job["stamp"] != self._stamp():
            return
        if job["mode"] == "probe":
            self.health.observe(job["source"]["identity"], result)
            if not self._probe_queue:
                self.probe_timer.start(self.PROBE_INTERVAL_MS)
            return
        req = self.request
        if req is None or job["serial"] != req["serial"]:
            return
        if self.clock() >= req["deadline"]:
            self._expire()
            return
        self.health.observe(job["source"]["identity"], result)
        req["done"] += 1
        req["failed"] += result["status"] == "failed"
        req["timed_out"] += result.get("error") in (
            "Timeout", "ConnectTimeout", "ReadTimeout", "ReadTimeoutError", "TimeoutError")
        req["empty"] += result["status"] == "empty"
        req["limited"] |= bool(result.get("limited"))
        rows = []
        for raw in result["items"]:
            item = dict(raw, _source=job["source"]["name"], _source_id=job["source"]["identity"])
            identity = (item["_source_id"], str(item.get("vod_id")),
                        hashlib.sha256(json.dumps([item.get("play_url"), item.get("first_url"), raw],
                                                    sort_keys=True).encode()).hexdigest())
            if identity in req["seen"]:
                continue
            if len(req["seen"]) >= self.MAX_CANDIDATES:
                req["limited"] = True
                break
            req["seen"].add(identity)
            work = work_identity(item)
            if work:
                self._work_candidates.setdefault(work, []).append(item)
            if work and req["groups"][work] >= 3:
                self.alternatives.append(item)
                continue
            if work:
                req["groups"][work] += 1
            rows.append(item)
        req["shown"] += len(rows)
        logger.info("movie_search_source serial=%s source_tag=%s result=%s count=%s elapsed_ms=%s error_type=%s",
                    req["serial"], job["source"]["identity"][:12], result["status"], len(rows),
                    result.get("total_elapsed_ms"), result.get("error", "none"))
        self.batch.emit(req["serial"], dict(source_id=job["source"]["identity"],
                                           source_name=job["source"]["name"], items=rows,
                                           status=result["status"], error=result.get("error"),
                                           elapsed_ms=result.get("elapsed_ms"),
                                           **self._counts(req)))

    def switch_candidates(self, item):
        group = self._work_candidates.get(work_identity(item), ())
        if not any(row.get("_source_id") == item.get("_source_id")
                   and row.get("vod_id") == item.get("vod_id")
                   and row.get("vod_play_url") == item.get("vod_play_url") for row in group):
            return ()
        return tuple(group)

    @staticmethod
    def _counts(req):
        return dict(done=req["done"], total=req["total"], failed=req["failed"],
                    attempted=req["attempted"], timed_out=req["timed_out"],
                    errors=req["failed"]-req["timed_out"],
                    unattempted=req["total"]-req["attempted"],
                    in_flight=req["attempted"]-req["done"])

    def _supplement(self):
        req = self.request
        if req and req["stamp"] == self._stamp():
            self.progress.emit(req["serial"], dict(phase="supplement", **self._counts(req)))

    def _expire(self):
        if self.request:
            self.pool.cancel()
            self._settle("budget")

    def _settle(self, reason):
        req, self.request = self.request, None
        if req:
            self.deadline_timer.stop()
            self.phase_timer.stop()
            if reason == "changed":
                self.pool.cancel()
            counts = self._counts(req)
            if reason == "budget":
                counts["timed_out"] += counts["in_flight"]
            self.completed.emit(req["serial"], dict(reason=reason, shown=req["shown"],
                                                     empty=req["empty"], limited=req["limited"], **counts))
            self.probe_timer.start(self.PROBE_INTERVAL_MS)

    def probe(self):
        if self.closed or self.request or self.pool.active or not self.manager or not self.idle():
            if not self.closed:
                self.probe_timer.start(self.PROBE_INTERVAL_MS)
            return
        self._probe_queue = deque(self._sources())
        self._probe_stamp = self._stamp()
        self._pump()

    def detach(self):
        self.invalidate()
        self._probe_queue.clear()
        self.probe_timer.stop()
        self.manager = None
        self.idle = lambda: False
        self.playback_rank = lambda _: 0

    def close(self):
        self.closed = True
        self.detach()
        self.pool.shutdown()


def shared_search_service():
    """One application owner: two slots across all UI generations and probes."""
    app = QCoreApplication.instance()
    if app is None:
        return None
    if (not getattr(sys, "frozen", False) and sys.platform == "win32"
            and not Path(sys.executable).with_name("pythonw.exe").is_file()):
        return None
    service = getattr(app, "_movie_progressive_search", None)
    if service is None:
        service = ProgressiveMovieSearch(app)
        app._movie_progressive_search = service
        app.aboutToQuit.connect(service.close)
    return service
