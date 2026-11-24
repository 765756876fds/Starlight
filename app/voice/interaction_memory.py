"""Bounded private interaction memory; one idle-exiting writer, no GUI disk I/O."""

from collections import deque
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
import threading
import time


log = logging.getLogger(__name__)


class InteractionMemory:
    LIMIT = 2000

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ready = threading.Condition(self._lock)
        self._tasks = deque()
        self._accepted = 0
        self._finished = 0
        self._worker = None
        self._closed = False
        self.available = True
        self._cache = []
        self._revoking = set()

    def start(self):
        return self._submit(("load", None))

    def _start_writer(self):
        self._worker = threading.Thread(target=self._drain, name="interaction-memory", daemon=True)
        self._worker.start()

    def _submit(self, task):
        with self._ready:
            if self._closed or not self.available or len(self._tasks) >= 64:
                return False
            self._tasks.append(task)
            self._accepted += 1
            if self._worker is None:
                self._start_writer()
            self._ready.notify_all()
            return True

    def record(
        self,
        event_id,
        *,
        feature,
        domain,
        action,
        media_key="",
        rating="",
        kind="observation",
        parent="",
    ):
        # All persisted language features come from finite concepts, not transcripts.
        if (
            domain not in ("music", "movie")
            or kind not in ("weak", "explicit", "correction", "confirmation", "observation")
            or not isinstance(feature, str)
            or len(feature) > 100
            or len(str(media_key)) > 100
        ):
            return False
        row = (
            str(event_id),
            feature,
            domain,
            str(action)[:32],
            str(media_key),
            rating,
            kind,
            0.1 if rating == "neutral" else 0.3 if rating == "negative" else 0.2,
            time.time(),
            str(parent),
        )
        return self._submit(("record", row))

    def revoke(self, event_id):
        if not event_id:
            return False
        with self._lock:
            self._revoking.add(str(event_id))
        if not self._submit(("revoke", str(event_id))):
            with self._lock:
                self._revoking.discard(str(event_id))
            return False
        return True

    def weak_profile(self, domain):
        with self._lock:
            rows = tuple(self._cache) if self.available else ()
            revoking = set(self._revoking)
        values = {}
        for row in rows:
            if row[2] != domain or row[6] != "weak" or row[10] or row[0] in revoking:
                continue
            sign = -1 if row[5] in ("neutral", "negative") else 1
            values[row[4]] = max(-1.5, min(1.5, values.get(row[4], 0) + sign * row[7]))
        return values

    def preferred_domain(self, feature, candidates):
        """Only multiple explicit corrections/confirmations, never self-approval."""
        with self._lock:
            rows = tuple(self._cache) if self.available else ()
        correction, confirmation = {}, {}
        for row in rows:
            if row[1] != feature or row[2] not in candidates or row[10]:
                continue
            bucket = (
                correction
                if row[6] == "correction"
                else confirmation
                if row[6] == "confirmation"
                else None
            )
            if bucket is not None:
                bucket[row[2]] = bucket.get(row[2], 0) + 1
        for bucket, minimum in ((correction, 2), (confirmation, 3)):
            eligible = [d for d, count in bucket.items() if count >= minimum]
            if len(eligible) == 1:
                return eligible[0]
        return None

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=0.15)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return db
            if version > 1:
                raise sqlite3.DatabaseError("future interaction memory schema")
            db.execute("BEGIN")
            db.execute("""CREATE TABLE IF NOT EXISTS interactions(
                id TEXT PRIMARY KEY,feature TEXT,domain TEXT,action TEXT,media_key TEXT,
                rating TEXT,kind TEXT,weight REAL,created REAL,parent TEXT,revoked INTEGER DEFAULT 0)""")
            db.execute("PRAGMA user_version=1")
            db.commit()
            return db
        except BaseException:
            db.close()
            raise

    def _drain(self):
        try:
            with closing(self._connect()) as db:
                while True:
                    with self._ready:
                        self._ready.wait_for(lambda: self._tasks or self._closed, timeout=0.5)
                        if not self._tasks:
                            return
                        batch = list(self._tasks)
                        self._tasks.clear()
                    if any(kind != "load" for kind, _ in batch):
                        with db:
                            for kind, payload in batch:
                                if kind == "record":
                                    db.execute(
                                        "INSERT OR IGNORE INTO interactions VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                                        payload,
                                    )
                                elif kind == "revoke":
                                    db.execute(
                                        "UPDATE interactions SET revoked=1 WHERE id=? AND kind='weak'",
                                        (payload,),
                                    )
                            db.execute(
                                "DELETE FROM interactions WHERE id NOT IN (SELECT id FROM interactions ORDER BY created DESC LIMIT ?)",
                                (self.LIMIT,),
                            )
                    rows = db.execute("SELECT * FROM interactions ORDER BY created").fetchall()
                    with self._ready:
                        self._cache = rows
                        for kind, payload in batch:
                            if kind == "revoke":
                                self._revoking.discard(payload)
                        self._finished += len(batch)
                        self._ready.notify_all()
        except (OSError, sqlite3.Error):
            with self._ready:
                self.available = False
                self._cache.clear()
                self._revoking.clear()
                self._tasks.clear()
                self._ready.notify_all()
            log.warning("interaction_memory_unavailable; original databases untouched", exc_info=True)
            from ..core.notifications import notify
            notify(
                "交互习惯暂时无法保存",
                "音乐和电影仍可使用，请稍后检查用户数据目录",
                status="warning",
                dedup_key="interaction-memory-unavailable",
            )
        finally:
            # Ownership transfers only after the old connection has been closed.
            with self._ready:
                self._worker = None
                if self._tasks and self.available:
                    self._start_writer()
                self._ready.notify_all()

    def flush(self, timeout=1):
        """Wait for this caller's accepted writes and their published cache."""
        with self._ready:
            target = self._accepted
            done = self._ready.wait_for(
                lambda: self._finished >= target or not self.available or self._worker is None,
                timeout=timeout,
            )
            return done and self.available and self._finished >= target

    def close(self, timeout=0.75):
        with self._ready:
            self._closed = True
            self._ready.notify_all()
            if self._worker is threading.current_thread():
                return False
            return self._ready.wait_for(lambda: self._worker is None, timeout=timeout)
