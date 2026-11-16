"""Opt-in LISTEN_MUSIC admission owned by MusicController; no voice registration.

Lock order: controller -> database -> short receipt lock. Receipt locks never
wait for controller/database/IPC. No arbitrary caller callback runs under locks.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid

from .preferences import media_key


class AdmissionStatus(str, Enum):
    REJECTED_STALE = "rejected_stale"
    REJECTED_UNAVAILABLE = "rejected_unavailable"
    ACCEPTED = "accepted"
    FAILED_NO_EFFECT = "failed_no_effect"
    UNKNOWN_EFFECTS = "unknown_effects"
    STARTED = "started"


@dataclass(frozen=True)
class StateToken:
    owner: str
    revision: int
    request: int
    session: int
    content: str
    queue_revision: int
    paused: bool
    stopped: bool
    source: tuple
    captured: float
    media_state: str
    entry: int | None


@dataclass(frozen=True)
class MemberToken:
    playlist_id: int
    member_id: int
    identity: str
    file_identity: tuple = field(repr=False)


@dataclass(frozen=True)
class GoalRequest:
    operation_id: str
    plan: str
    expected: StateToken
    deadline: float
    member: MemberToken | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    goal: str = "LISTEN_MUSIC"


@dataclass(frozen=True)
class PlaybackProof:
    entry_id: int
    previous_position: float
    position: float
    observed_at: float
    content_identity: str


@dataclass(frozen=True)
class GoalReceipt:
    operation_id: str
    status: AdmissionStatus
    reason: str
    effects_started: bool = False
    mpv_accepted: bool = False
    epoch: int | None = None
    proof: PlaybackProof | None = None

    @property
    def safe_to_fallback(self):
        return (
            self.status is AdmissionStatus.FAILED_NO_EFFECT
            and not self.effects_started
            and self.reason == "resource_not_attemptable"
        )


class SnapshotError(RuntimeError):
    pass


class StaleAdmission(SnapshotError):
    pass


def require_worker():
    qt = sys.modules.get("PySide6.QtCore")
    if qt is not None and qt.QCoreApplication.instance() is not None:
        if qt.QThread.currentThread() == qt.QCoreApplication.instance().thread():
            raise SnapshotError("goal_requires_worker")


def file_identity(path):
    p = Path(path)
    stat = p.stat()
    if not p.is_file() or stat.st_size <= 0:
        raise OSError("not_readable_media")
    with p.open("rb") as stream:
        if not stream.read(1):
            raise OSError("empty_media")
    return (
        os.path.normcase(str(p.resolve())),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )


@contextmanager
def member_lease(database, playlist_id, member_id, deadline):
    """Authority read on the owner's connection; no migration/transaction repair.

    Only this lease's reader changes busy_timeout, restoring the exact value.
    Existing managed DB mutations use the same RLock. External SQL/filesystem
    changes are not a distributed transaction with MPV.
    """

    def remaining():
        return max(0, deadline - time.monotonic())

    if not database._lock.acquire(timeout=min(0.02, remaining())):
        raise SnapshotError("database_busy")
    db, old, owns_window = None, None, False
    try:
        db = database._conn
        if db is None or db.in_transaction:
            raise SnapshotError("database_closed_or_transaction_busy")
        owns_window = True
        old = db.execute("PRAGMA busy_timeout").fetchone()[0]
        db.execute(f"PRAGMA busy_timeout={min(20, int(remaining() * 1000))}")
        if db.execute("PRAGMA user_version").fetchone()[0] != 0:
            raise SnapshotError("database_schema_unknown")
        row = db.execute(
            """SELECT s.*, p.created_at AS playlist_birth,
            m.path AS local_path, m.filename AS local_filename, m.title AS local_title,
            m.artist AS local_artist, m.album AS local_album, m.format AS local_format,
            m.duration AS local_duration FROM playlist_songs s
            JOIN playlists p ON p.id=s.playlist_id LEFT JOIN music m ON m.id=s.music_id
            WHERE s.playlist_id=? AND s.id=?""",
            (playlist_id, member_id),
        ).fetchone()
        if row is None:
            yield None, None
        else:
            values = dict(row)
            identity = hashlib.sha256(
                json.dumps(values, sort_keys=True, default=str).encode()
            ).hexdigest()
            yield values, identity
    except sqlite3.Error as exc:
        raise SnapshotError("database_unavailable") from exc
    finally:
        try:
            if db is not None and owns_window and db.in_transaction:
                db.rollback()  # Only an uncommitted transaction begun in our exclusive window.
            if db is not None and old is not None:
                db.execute(f"PRAGMA busy_timeout={int(old)}")
        finally:
            database._lock.release()


@dataclass
class _Operation:
    request: GoalRequest
    receipt: GoalReceipt
    epoch: int
    processing: bool = True
    path: str = field(default="", repr=False)
    position: float | None = None
    guard: object = field(default=None, repr=False)


class GoalAdmission:
    """Created only by explicit MusicController.goal_* calls. No worker pool."""

    def __init__(self, controller):
        self.controller = controller
        self.identity = uuid.uuid4().hex
        self.revision = 0
        self.closed = False
        self._lock = threading.RLock()
        self._records = {}
        self._active = None
        self._local = threading.local()
        self._queue_revision = controller.queue._revision
        self._blocked_auto_epoch = None

    def _source(self):
        manager = self.controller.online_manager
        if manager is None:
            return ()
        if not manager._life_lock.acquire(timeout=0.02):
            raise SnapshotError("source_busy")
        try:
            return (
                id(manager),
                tuple(sorted(manager._source_versions.items())),
                manager._executor._shutdown,
                tuple(sorted(manager._sources)),
            )
        finally:
            manager._life_lock.release()

    def _stamp(self):
        c = self.controller
        if not c.queue._lock.acquire(timeout=0.02):
            raise SnapshotError("queue_busy")
        try:
            return (
                self.revision,
                c._play_request_id,
                c._playback_gen,
                media_key(c._current_song) if c._current_song else "",
                c.queue._revision,
                c._paused_snapshot,
                c._user_stopped,
                self._source(),
            )
        finally:
            c.queue._lock.release()

    def user_action(self, guard=None, action=""):
        with self._lock:
            op = self._local.__dict__.get("operation")
            if op is not None and guard is op.guard:
                return
            self.revision += 1
            if action in ("play", "resume", "toggle"):
                self._blocked_auto_epoch = None
            if self._active is not None:
                self._stale(self._active, "new_user_action")

    def queue_changed(self):
        revision = self.controller.queue._revision
        with self._lock:
            if revision == self._queue_revision:
                return
            self._queue_revision = revision
            if getattr(self._local, "operation", None) is not self._active or self._active is None:
                self.user_action()

    def bind_epoch(self, epoch):
        with self._lock:
            op = getattr(self._local, "operation", None)
            if op is not None and op is self._active:
                op.epoch = epoch
                self._blocked_auto_epoch = epoch
            elif self._active is not None:
                self._stale(self._active, "new_playback_epoch")

    def blocks_auto_advance(self):
        with self._lock:
            return self._blocked_auto_epoch == self.controller._play_request_id

    def _stale(self, op, reason):
        op.receipt = GoalReceipt(
            op.request.operation_id,
            AdmissionStatus.REJECTED_STALE,
            reason,
            op.receipt.effects_started,
            op.receipt.mpv_accepted,
            op.epoch,
        )
        if not op.processing:
            self._active = None

    def current(self, op):
        with self._lock:
            valid = (
                not self.closed
                and self._active is op
                and op.receipt.status is not AdmissionStatus.REJECTED_STALE
                and self.revision == op.request.expected.revision
                and not op.request.cancel.is_set()
                and time.monotonic() < op.request.deadline
                and self.controller._play_request_id == op.epoch
            )
        if not valid:
            return False
        try:
            member = op.request.member
            return (
                self._source() == op.request.expected.source
                and (
                    op.request.plan == "NO_OP"
                    or self.controller.state_store.get("movie", "playing", None) is False
                )
                and (
                    member is None or file_identity(member.file_identity[0]) == member.file_identity
                )
            )
        except (SnapshotError, OSError):
            return False

    def report(self, stage, result, epoch=None):
        with self._lock:
            op = getattr(self._local, "operation", None)
            if op is not self._active or op is None or epoch != op.epoch or stage != "loadfile":
                return
            if result == "confirmed":
                op.receipt = GoalReceipt(
                    op.request.operation_id,
                    AdmissionStatus.REJECTED_STALE
                    if op.receipt.status is AdmissionStatus.REJECTED_STALE
                    else AdmissionStatus.ACCEPTED,
                    "mpv_load_accepted_not_started",
                    True,
                    True,
                    op.epoch,
                )
            else:
                self._unknown(op, "loadfile_failed_or_unconfirmed")

    def owned_operation(self):
        return getattr(self._local, "operation", None)

    def _property(self, name):
        return self.controller._next_feedback._property(name)

    def snapshot(self):
        require_worker()
        c = self.controller
        if not c._lock.acquire(timeout=0.02):
            raise SnapshotError("controller_busy")
        try:
            before = self._stamp()
        finally:
            c._lock.release()
        idle, path, paused, position = (
            self._property(k) for k in ("idle-active", "path", "pause", "time-pos")
        )
        entry = c._next_feedback._entry()
        if not c._lock.acquire(timeout=0.02):
            raise SnapshotError("controller_busy")
        try:
            if before != self._stamp():
                raise SnapshotError("state_changed_during_snapshot")
            state = "stopped" if idle is True else "unknown"
            if (
                idle is False
                and path == (c._current_song or {}).get("path")
                and type(paused) is bool
                and type(position) in (int, float)
                and math.isfinite(position)
                and position >= 0
            ):
                state = "paused" if paused else "playing"
            return StateToken(
                self.identity, *before, time.monotonic(), state, entry.get("id") if entry else None
            )
        finally:
            c._lock.release()

    def member(self, playlist_id, member_id):
        require_worker()
        with self.controller.db.playback_member_lease(
            playlist_id, member_id, time.monotonic() + 0.15
        ) as (row, identity):
            if row is None or row["source_type"] != "local" or not row["local_path"]:
                raise SnapshotError("local_member_unavailable")
            return MemberToken(playlist_id, member_id, identity, file_identity(row["local_path"]))

    def admit(self, request):
        if not isinstance(request, GoalRequest) or not isinstance(request.expected, StateToken):
            return GoalReceipt("", AdmissionStatus.REJECTED_UNAVAILABLE, "unsupported_request")
        try:
            require_worker()
        except SnapshotError:
            return GoalReceipt(
                request.operation_id, AdmissionStatus.REJECTED_UNAVAILABLE, "goal_requires_worker"
            )
        c = self.controller
        if (
            not isinstance(request, GoalRequest)
            or not isinstance(request.operation_id, str)
            or not request.operation_id
            or len(request.operation_id) > 128
            or request.goal != "LISTEN_MUSIC"
            or request.plan not in ("NO_OP", "RESUME_MUSIC", "TRY_COLLECTION", "TRY_CHART")
            or not isinstance(request.cancel, threading.Event)
        ):
            return GoalReceipt(
                getattr(request, "operation_id", ""),
                AdmissionStatus.REJECTED_UNAVAILABLE,
                "unsupported_request",
            )

        def reject(why):
            return GoalReceipt(request.operation_id, AdmissionStatus.REJECTED_STALE, why)

        with self._lock:
            old = self._records.get(request.operation_id)
            if old:
                return old.receipt if old.request == request else reject("operation_id_reused")
        if (
            type(request.deadline) not in (int, float)
            or not math.isfinite(request.deadline)
            or not 0 < request.deadline - time.monotonic() <= 5
            or request.cancel.is_set()
        ):
            return reject("cancelled_or_deadline_invalid")
        if not c._lock.acquire(timeout=min(0.02, max(0, request.deadline - time.monotonic()))):
            return GoalReceipt(
                request.operation_id, AdmissionStatus.REJECTED_UNAVAILABLE, "controller_busy"
            )
        op = None
        try:
            with self._lock:
                old = self._records.get(request.operation_id)
                if old:
                    return old.receipt if old.request == request else reject("operation_id_reused")
                if self._active is not None:
                    return GoalReceipt(
                        request.operation_id,
                        AdmissionStatus.REJECTED_UNAVAILABLE,
                        "previous_operation_unresolved",
                    )
            expected = request.expected
            if (
                self.closed
                or expected.owner != self.identity
                or self._stamp()
                != (
                    expected.revision,
                    expected.request,
                    expected.session,
                    expected.content,
                    expected.queue_revision,
                    expected.paused,
                    expected.stopped,
                    expected.source,
                )
            ):
                return reject("state_or_source_changed")
            if not 0 <= time.monotonic() - expected.captured <= 1 or c._is_switching_track:
                return reject("snapshot_expired_or_switching")
            if (
                not c._running
                or not c._mpv_available
                or (
                    request.plan != "NO_OP"
                    and c.state_store.get("movie", "playing", None) is not False
                )
            ):
                return GoalReceipt(
                    request.operation_id,
                    AdmissionStatus.REJECTED_UNAVAILABLE,
                    "player_or_media_permission_unavailable",
                )
            if c._mpv_restart_pending or c._candidate_switching or c.recommendations._busy:
                return GoalReceipt(
                    request.operation_id,
                    AdmissionStatus.REJECTED_UNAVAILABLE,
                    "existing_business_in_progress",
                )
            if request.plan == "TRY_CHART" and (
                not expected.source or expected.source[2] is not False or not expected.source[3]
            ):
                return GoalReceipt(
                    request.operation_id,
                    AdmissionStatus.REJECTED_UNAVAILABLE,
                    "source_owner_unavailable",
                )
            if request.plan in ("NO_OP", "RESUME_MUSIC"):
                wanted = "playing" if request.plan == "NO_OP" else "paused"
                if expected.media_state != wanted or not c._current_song:
                    return reject("session_not_reliable")
            if request.plan in ("TRY_COLLECTION", "TRY_CHART") and not callable(
                getattr(c.mpv, "loadfile_guarded", None)
            ):
                return GoalReceipt(
                    request.operation_id,
                    AdmissionStatus.REJECTED_UNAVAILABLE,
                    "guarded_player_required",
                )
            if request.plan == "TRY_COLLECTION":
                if request.member is None or not callable(getattr(c.mpv, "loadfile_guarded", None)):
                    return GoalReceipt(
                        request.operation_id,
                        AdmissionStatus.REJECTED_UNAVAILABLE,
                        "guarded_local_member_required",
                    )
                with c.db.playback_member_lease(
                    request.member.playlist_id, request.member.member_id, request.deadline
                ) as (row, identity):
                    if row is None or identity != request.member.identity:
                        return reject("member_changed")
                    if (
                        row["source_type"] != "local"
                        or file_identity(row["local_path"]) != request.member.file_identity
                    ):
                        return reject("resource_changed")
                    song = c.db.get_song_by_id(row["music_id"])
                    op = self._claim(request)
                    with self._scope(op):
                        if not self.current(op):
                            self._stale(op, "cancelled_before_effects")
                        else:
                            if self._effects(op):
                                c._cancel_candidate_switch()
                                c.recommendations.cancel(
                                    clear_session=True, reason="goal_collection"
                                )
                                c._play_selected_song(song, _admission=op)
            else:
                op = self._claim(request)
        except StaleAdmission:
            return reject("stale_at_claim")
        except OSError:
            if op is None:
                return GoalReceipt(
                    request.operation_id,
                    AdmissionStatus.FAILED_NO_EFFECT,
                    "resource_not_attemptable",
                )
            self._unknown(op, "business_interrupted")
        except (SnapshotError, sqlite3.Error):
            if op is None:
                return GoalReceipt(
                    request.operation_id,
                    AdmissionStatus.FAILED_NO_EFFECT,
                    "authority_read_unavailable",
                )
            self._unknown(op, "business_interrupted")
        except Exception:
            if op is None:
                return GoalReceipt(
                    request.operation_id,
                    AdmissionStatus.FAILED_NO_EFFECT,
                    "admission_failed_before_effects",
                )
            self._unknown(op, "business_interrupted")
        finally:
            c._lock.release()
        if op is None:
            return reject("admission_not_completed")
        try:
            if request.plan == "RESUME_MUSIC":
                with self._scope(op):
                    if self._effects(op):
                        c.resume(_voice_guard=op.guard)
            elif request.plan == "TRY_CHART":
                with self._scope(op):
                    if self._effects(op):
                        c.recommend_music(expected_request=expected.request, is_current=op.guard)
            live = self.current(op)
            with self._lock:
                if (
                    not live
                    or self.revision != request.expected.revision
                    or op.receipt.status is AdmissionStatus.REJECTED_STALE
                ):
                    if time.monotonic() >= request.deadline and not request.cancel.is_set():
                        self._unknown(op, "request_deadline_after_acceptance")
                    else:
                        self._stale(op, "cancelled_or_state_changed")
                op.path = (c._current_song or {}).get("path", "")
                return op.receipt
        except Exception:
            self._unknown(op, "business_interrupted")
            return op.receipt
        finally:
            with self._lock:
                op.processing = False
                if op.receipt.status is AdmissionStatus.REJECTED_STALE:
                    self._active = None

    def _claim(self, request):
        with self._lock:
            if (
                self.revision != request.expected.revision
                or request.cancel.is_set()
                or time.monotonic() >= request.deadline
            ):
                raise StaleAdmission("stale_at_claim")
            op = _Operation(
                request,
                GoalReceipt(
                    request.operation_id,
                    AdmissionStatus.ACCEPTED,
                    "admitted_not_started",
                    epoch=request.expected.request,
                ),
                request.expected.request,
            )
            op.guard = lambda: self._continuation_allowed(op)
            self._records[request.operation_id] = op
            self._active = op  # Admission linearization; controller lock is held.
            if len(self._records) > 128:
                self._records.pop(next(iter(self._records)))
            return op

    def _continuation_allowed(self, op):
        if not self.current(op):
            return False
        with self._lock:
            # Stop even the existing recommender's internal retries after an
            # unconfirmed load. Observation may still establish what MPV did.
            return op.receipt.status is not AdmissionStatus.UNKNOWN_EFFECTS

    @contextmanager
    def _scope(self, op):
        self._local.operation = op
        try:
            yield
        finally:
            self._local.operation = None

    def _effects(self, op):
        if not self.current(op):
            with self._lock:
                self._stale(op, "stale_before_business")
            return False
        with self._lock:
            if op.receipt.status is AdmissionStatus.REJECTED_STALE:
                return False
            op.receipt = GoalReceipt(
                op.request.operation_id,
                AdmissionStatus.ACCEPTED,
                "business_started_not_playback_proven",
                True,
                epoch=op.epoch,
            )
            self._blocked_auto_epoch = op.epoch
            return True

    def _unknown(self, op, why):
        with self._lock:
            if op.receipt.status is AdmissionStatus.REJECTED_STALE:
                return
            op.receipt = GoalReceipt(
                op.request.operation_id,
                AdmissionStatus.UNKNOWN_EFFECTS,
                why,
                op.receipt.effects_started,
                op.receipt.mpv_accepted,
                op.epoch,
            )

    def cancel(self, operation_id):
        with self._lock:
            op = self._records.get(operation_id)
            if op is None:
                return None
            op.request.cancel.set()
            self._stale(op, "explicit_cancel")
            return op.receipt

    def close(self):
        with self._lock:
            self.closed = True
            self.revision += 1
            if self._active:
                self._stale(self._active, "controller_closed")

    def observe(self, operation_id):
        require_worker()
        with self._lock:
            op = self._records.get(operation_id)
            if op is None:
                return None
            if op.receipt.status in (AdmissionStatus.REJECTED_STALE, AdmissionStatus.STARTED):
                return op.receipt
        if not self.current(op):
            with self._lock:
                if time.monotonic() >= op.request.deadline and not op.request.cancel.is_set():
                    self._unknown(op, "confirmation_deadline_unresolved")
                else:
                    self._stale(op, "expired_cancelled_or_superseded")
                return op.receipt
        c = self.controller
        if op.request.member is not None:
            try:
                member = self.member(op.request.member.playlist_id, op.request.member.member_id)
                if member != op.request.member:
                    self.cancel(operation_id)
                    return op.receipt
            except OSError:
                self.cancel(operation_id)
                return op.receipt
            except SnapshotError as exc:
                if str(exc) == "local_member_unavailable":
                    self.cancel(operation_id)
                    return op.receipt
                self._unknown(op, "member_revalidation_unavailable")
                return op.receipt
        try:
            path, position, idle, paused = (
                self._property(k) for k in ("path", "time-pos", "idle-active", "pause")
            )
            entry = c._next_feedback._entry()
        except (OSError, TimeoutError, ValueError):
            self._unknown(op, "mpv_observation_unavailable")
            return op.receipt
        changed_entry = (
            entry is not None
            and entry.get("filename") == op.path
            and (
                (
                    entry.get("id") == op.request.expected.entry
                    and op.request.expected.entry is not None
                )
                if op.request.plan in ("NO_OP", "RESUME_MUSIC")
                else entry.get("id") != op.request.expected.entry
            )
        )
        valid_position = (
            type(position) in (int, float) and math.isfinite(position) and position >= 0
        )
        live = self.current(op)
        with self._lock:
            if (
                not live
                or self.revision != op.request.expected.revision
                or op.receipt.status is AdmissionStatus.REJECTED_STALE
            ):
                self._stale(op, "changed_during_mpv_read")
            elif (
                path == op.path
                and idle is False
                and paused is False
                and changed_entry
                and valid_position
                and op.position is not None
                and position > op.position
            ):
                op.receipt = GoalReceipt(
                    operation_id,
                    AdmissionStatus.STARTED,
                    "matching_mpv_entry_progress",
                    op.receipt.effects_started,
                    op.receipt.mpv_accepted,
                    op.epoch,
                    PlaybackProof(
                        entry["id"],
                        op.position,
                        position,
                        time.monotonic(),
                        hashlib.sha256(op.path.encode()).hexdigest(),
                    ),
                )
                self._blocked_auto_epoch = None
                self._active = None
            elif valid_position:
                op.position = position
            return op.receipt
