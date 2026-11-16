"""Experimental A/B SQLite snapshots. Never construct or modify MusicDatabase.

A uses a dedicated normal read-only WAL-aware connection, not immutable=1.
B accepts ONLY LabOwner, whose callbacks and connection are owned by this lab.
"""
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
import threading
import time

from tools.goal_shadow.readonly import opaque, off_gui_thread, song_identity


@dataclass(frozen=True)
class Member:
    ref: str
    collection_ref: str
    content_ref: str
    kind: str
    owned: bool
    explicit_saved: bool
    path: str = field(default="", repr=False)
    source: str = field(default="", repr=False)
    track_id: str = field(default="", repr=False)


@dataclass(frozen=True)
class CollectionSnapshot:
    status: str
    reason: str
    rows: tuple[Member, ...] = ()
    total: int | None = None
    complete: bool = False
    file_identity: tuple = ()
    owner_epoch: int | None = None
    elapsed_ms: float = 0


def file_identity(path):
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino


def _authorizer(op, a, b, *_):
    if op in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    if op == sqlite3.SQLITE_TRANSACTION and a in ("BEGIN", "ROLLBACK"):
        return sqlite3.SQLITE_OK
    if op == sqlite3.SQLITE_PRAGMA and a == "user_version" and b is None:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _read_transaction(db, *, deadline, cancel, limit, owned, pinned=None):
    if time.monotonic() >= deadline or cancel.is_set():
        raise TimeoutError("query_cancelled_or_expired")
    db.execute("BEGIN")
    try:
        # The first read pins a WAL snapshot. BEGIN alone does not pin it.
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != 0:
            return CollectionSnapshot("unverified", "unknown_schema_version")
        total = db.execute("SELECT COUNT(*) FROM playlist_songs s JOIN playlists p ON p.id=s.playlist_id").fetchone()[0]
        if pinned is not None:
            pinned()
        if time.monotonic() >= deadline or cancel.is_set():
            raise TimeoutError("query_cancelled_or_expired")
        rows = db.execute("""SELECT s.id,s.playlist_id,s.music_id,s.source_type,s.source_name,
                       s.track_id,m.path,p.name FROM playlist_songs s
                       JOIN playlists p ON p.id=s.playlist_id
                       LEFT JOIN music m ON m.id=s.music_id AND s.source_type='local'
                       ORDER BY s.playlist_id,s.id LIMIT ?""", (limit+1,)).fetchall()
        members = tuple(Member(opaque((pid,sid)), opaque(pid),
            song_identity(dict(music_id=mid,source_name=source,track_id=track)), kind, owned,
            name == "我喜欢的音乐", str(path or ""), str(source or ""), str(track or ""))
            for sid,pid,mid,kind,source,track,path,name in rows[:limit])
        if time.monotonic() >= deadline or cancel.is_set():
            raise TimeoutError("query_cancelled_or_expired")
        complete = len(rows) <= limit and total == len(members)
        return CollectionSnapshot("ok" if complete else "partial", "snapshot_complete" if complete else "bounded_prefix_only",
                                  members, total, complete)
    finally:
        db.set_progress_handler(None,0)  # Cancellation must not interrupt cleanup.
        db.execute("ROLLBACK")  # Only the read transaction started above.


class ActiveReadSnapshot:
    """Dedicated worker-only mode=ro connection; no shared cache, writes or repair."""
    def __init__(self, path, *, trusted_personal=False, limit=64):
        self.path = Path(path)
        self.owned, self.limit = trusted_personal, min(max(int(limit),1),256)
        self._identity = None
        self.active_connections = 0
        self._gate = threading.Lock()

    def read(self, *, budget=.2, cancel=None, pinned=None):
        off_gui_thread()
        if not self._gate.acquire(blocking=False):
            return CollectionSnapshot("unverified", "reader_busy")
        started = time.perf_counter_ns()
        deadline = time.monotonic()+min(max(budget,0),1)
        cancel = cancel or threading.Event()
        db = None
        try:
            identity = file_identity(self.path)
            if self._identity is not None and self._identity != identity:
                return CollectionSnapshot("unverified", "database_replaced")
            if cancel.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("query_cancelled_or_expired")
            db = sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True,
                                 timeout=min(.02,max(0,deadline-time.monotonic())), isolation_level=None)
            self.active_connections += 1
            db.set_authorizer(_authorizer)
            db.set_progress_handler(lambda:int(cancel.is_set() or time.monotonic()>=deadline),100)
            result = _read_transaction(db,deadline=deadline,cancel=cancel,limit=self.limit,owned=self.owned,pinned=pinned)
            if file_identity(self.path) != identity:
                return CollectionSnapshot("unverified", "database_replaced")
            self._identity = identity
            from dataclasses import replace
            return replace(result,file_identity=identity,elapsed_ms=(time.perf_counter_ns()-started)/1e6)
        except FileNotFoundError:
            return CollectionSnapshot("unverified", "database_missing")
        except TimeoutError:
            return CollectionSnapshot("unverified", "query_cancelled_or_expired")
        except (sqlite3.Error,OSError):
            return CollectionSnapshot("unverified", "sqlite_busy_unreadable_or_changed")
        finally:
            if db is not None:
                db.close()
                self.active_connections -= 1
            self._gate.release()


class LabOwner:
    """Temporary-database owner for B comparison, NOT a production owner adapter.

    The caller supplies a lab-created connection. This class never opens paths.
    """
    def __init__(self, connection):
        self.connection = connection
        self.lock = threading.RLock()
        self.epoch = 0

    def close(self, timeout=.2):
        if not self.lock.acquire(timeout=timeout):
            return False
        try:
            self.epoch += 1
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            return True
        finally:
            self.lock.release()


class OwnerReadSnapshot:
    """Model of a future owner API; it refuses production MusicDatabase objects."""
    def __init__(self, owner, *, trusted_personal=False, limit=64):
        if type(owner) is not LabOwner:
            raise TypeError("lab_owner_only_no_production_connection_mutation")
        self.owner, self.owned, self.limit = owner, trusted_personal, min(max(int(limit),1),256)

    def read(self, *, budget=.2, cancel=None, pinned=None):
        off_gui_thread()
        started = time.perf_counter_ns()
        deadline = time.monotonic()+min(max(budget,0),1)
        cancel = cancel or threading.Event()
        if not self.owner.lock.acquire(timeout=min(.02,max(0,budget))):
            return CollectionSnapshot("unverified", "owner_lock_busy")
        db = None
        old_busy_timeout = None
        try:
            db = self.owner.connection
            if db is None:
                return CollectionSnapshot("unverified", "owner_closed")
            if db.in_transaction:
                return CollectionSnapshot("unverified", "owner_transaction_busy")
            old_busy_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
            busy_ms = min(20,max(0,int((deadline-time.monotonic())*1000)))
            db.execute(f"PRAGMA busy_timeout={busy_ms}")
            db.set_authorizer(_authorizer)
            db.set_progress_handler(lambda:int(cancel.is_set() or time.monotonic()>=deadline),100)
            result = _read_transaction(db,deadline=deadline,cancel=cancel,limit=self.limit,owned=self.owned,pinned=pinned)
            from dataclasses import replace
            return replace(result,owner_epoch=self.owner.epoch,elapsed_ms=(time.perf_counter_ns()-started)/1e6)
        except (sqlite3.Error, OSError, TimeoutError):
            return CollectionSnapshot("unverified", "owner_query_cancelled_busy_or_changed")
        finally:
            if db is not None:
                # LabOwner explicitly owns these callbacks. There is no getter
                # to restore arbitrary callbacks on a real production connection.
                db.set_progress_handler(None,0)
                db.set_authorizer(None)
                if old_busy_timeout is not None:
                    db.execute(f"PRAGMA busy_timeout={int(old_busy_timeout)}")
            self.owner.lock.release()
