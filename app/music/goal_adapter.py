"""Default-off B5.2 adapter, restricted to explicitly supplied isolated data roots.

No registration, own threads, player implementation, or online preparation.
All admitted effects belong to the controller's B4 interface.
"""

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .goal_admission import (
    AdmissionStatus as S,
    GoalReceipt,
    GoalRequest,
    SnapshotError,
    require_worker,
)
from .goal_decision import Candidate, Decision, decide
from .preferences import media_key, normalize
from .recommendation import style_score


@dataclass(frozen=True)
class Resources:
    candidates: tuple[Candidate, ...]
    fingerprint: str
    reliable: bool = True
    collection_complete: bool = True
    database_revision: tuple | None = None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class ReadonlyResources:
    """Bounded normal WAL reads. Never construct PreferenceStore/MusicDatabase."""

    collection_authorized = True

    def __init__(self, controller, root):
        self.c, self.root = controller, Path(root).resolve()

    def inside(self, path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise SnapshotError("outside_isolation_root")
        return resolved

    def _query(self, path, version, query, deadline, cancel, *, missing_ok=False):
        path = self.inside(path)
        if not path.is_file():
            if missing_ok:
                return (), "missing"
            raise SnapshotError("database_missing")
        before = path.stat()
        db = None
        try:
            db = sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, timeout=0.02, isolation_level=None
            )
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(
                lambda: int(cancel.is_set() or time.monotonic() >= deadline), 100
            )
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != version:
                raise SnapshotError("database_schema_unknown")
            rows = tuple(db.execute(query).fetchall())
            after = path.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise SnapshotError("database_replaced")
            if cancel.is_set() or time.monotonic() >= deadline:
                raise SnapshotError("read_cancelled_or_expired")
            return rows, (after.st_dev, after.st_ino)
        finally:
            if db is not None:
                db.set_progress_handler(None, 0)
                if db.in_transaction:
                    db.rollback()
                db.close()

    def capture(self, expected, cancel, deadline):
        require_worker()
        c = self.c
        self.inside(c.db_path)
        self.inside(c.music_memory.path)
        deadline = min(deadline, time.monotonic() + 0.25)
        changes = self._database_revision()
        rows, db_identity = self._query(
            c.db_path,
            0,
            """
            SELECT s.playlist_id,s.id,m.id,m.path,m.title,m.artist,m.album,m.duration,
                   m.genre,m.year FROM playlist_songs s JOIN playlists p ON p.id=s.playlist_id
            JOIN music m ON m.id=s.music_id WHERE s.source_type='local'
            ORDER BY s.playlist_id,s.id LIMIT 65""",
            deadline,
            cancel,
        ) if self.collection_authorized else ((), "collection_not_authorized")
        preferences, pref_identity = self._query(
            c.music_memory.path,
            1,
            "SELECT kind,value,weight FROM preferences WHERE domain='music' ORDER BY kind,value LIMIT 513",
            deadline,
            cancel,
            missing_ok=True,
        ) if self.collection_authorized else ((), "preferences_not_read")
        if len(preferences) > 512:
            raise SnapshotError("preference_snapshot_partial")
        explicit = {(kind, value): weight for kind, value, weight in preferences}
        candidates = []
        for pid, mid, music_id, path, title, artist, album, duration, genre, year in rows[:64]:
            try:
                media = self.inside(path)
                if media.suffix.lower() not in c.extensions:
                    continue
                member = c.goal_member(pid, mid)
            except FileNotFoundError:
                candidates.append(Candidate("TRY_COLLECTION", "unavailable", reason="local_file_missing"))
                continue
            except (OSError, SnapshotError):
                candidates.append(
                    Candidate("TRY_COLLECTION", "unknown", reason="member_unverified")
                )
                continue
            song = dict(id=music_id, title=title, artist=artist, album=album, duration=duration)
            features = [("track", media_key(song)), ("artist", normalize(artist))]
            if str(year or "").isdigit() and 1900 <= int(year) <= 2100:
                features.append(("era", str(int(year) // 10 * 10)))
            weights = [explicit.get(pair, 0) for pair in features]
            # No invented tags or inferred listening weights. Style stays with
            # the existing recommender; a local filename is not mood evidence.
            candidates.append(
                Candidate(
                    "TRY_COLLECTION",
                    "attemptable",
                    member,
                    any(w > 0 for w in weights),
                    any(w < 0 for w in weights),
                    "owned_readable_local_not_decode_proof",
                )
            )
        manager = c.online_manager
        metadata, signature = (), ()
        if manager is not None:
            if not manager._life_lock.acquire(timeout=0.02):
                raise SnapshotError("source_busy")
            try:
                signature = (
                    id(manager),
                    tuple(sorted(manager._source_versions.items())),
                    manager._executor._shutdown,
                    tuple(sorted(manager._sources)),
                )
                metadata = tuple(
                    (
                        name,
                        all(
                            callable(getattr(src, method, None))
                            for method in ("get_top_lists", "get_top_list_detail", "get_play_url")
                        ),
                        tuple(
                            (
                                p.get("name"),
                                p.get("status"),
                                bool((p.get("capabilities") or {}).get("charts")),
                            )
                            for p in getattr(src, "_plugins", ())[:64]
                        ),
                    )
                    for name, src in list(manager._sources.items())[:64]
                )
            finally:
                manager._life_lock.release()
        if signature != expected.source:
            raise SnapshotError("source_changed")
        ready = any(
            capable and ok == "ok" and charts
            for _, capable, plugins in metadata
            for _, ok, charts in plugins
        )
        availability = (
            "attemptable"
            if ready and signature[2] is False
            else "unknown"
            if signature and signature[3] and signature[2] is False
            else "unavailable"
        )
        # Cold-start does not require a catalog. This macro prepares its own
        # candidates through the existing recommender, inside B4's deadline.
        chart_match, labels = False, ()
        service = c.recommendations
        if not service._lock.acquire(timeout=0.02):
            raise SnapshotError("recommendation_busy")
        try:
            if service._closed or service._busy:
                availability = "unknown"
            catalog = service._catalog
            if (
                catalog
                and signature
                and catalog[0] == (id(manager), signature[1])
                and 0 <= time.monotonic() - service._catalog_at <= 120
            ):
                labels = tuple(
                    str(item.get("title", ""))
                    for groups in list(catalog[1].values())[:16]
                    for group in groups[:4]
                    for item in group.get("lists", [])[:4]
                )
                chart_match = any(
                    kind == "style" and weight > 0 and style_score(label, value)
                    for kind, value, weight in preferences
                    for label in labels
                )
        finally:
            service._lock.release()
        candidates.append(
            Candidate(
                "TRY_CHART",
                availability,
                explicit_match=chart_match,
                reason="registered_chart_preparation_not_playback_proof",
            )
        )
        if cancel.is_set() or time.monotonic() >= deadline:
            raise SnapshotError("resource_snapshot_expired")
        fingerprint = _digest(
            (
                rows,
                db_identity,
                preferences,
                pref_identity,
                metadata,
                labels,
                [
                    (
                        x.plan,
                        x.availability,
                        x.explicit_match,
                        (x.member.identity, x.member.file_identity) if x.member else None,
                        x.excluded,
                    )
                    for x in candidates
                ],
            )
        )
        if self._database_revision() != changes:
            raise SnapshotError("database_changed_during_capture")
        return Resources(tuple(candidates), fingerprint, True, len(rows) <= 64, changes)

    def _database_revision(self):
        db = self.c.db
        if not db._lock.acquire(timeout=0.02):
            raise SnapshotError("database_busy")
        try:
            if db._conn is None:
                raise SnapshotError("database_closed")
            return (id(db), id(db._conn), db._conn.total_changes)
        finally:
            db._lock.release()


@dataclass(frozen=True)
class Ticket:
    generation: int
    operation_id: str
    goal: str
    cancel: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)


@dataclass(frozen=True)
class Prepared:
    state: object
    resources: Resources
    decision: Decision
    ticket: Ticket


@dataclass(frozen=True)
class AdapterResult:
    status: str
    reason: str
    decision: Decision | None = None
    receipt: GoalReceipt | None = None


class GoalAdapter:
    """Caller supplies a worker. One active operation, no queue/pool or auto-retry."""

    def __init__(self, controller, *, isolation_root=None, enabled=False, resources=None):
        self.c, self.enabled = controller, enabled
        self.resources = resources if resources is not None else ReadonlyResources(controller, isolation_root)
        self._lock, self._gate = threading.Lock(), threading.Lock()
        self._generation, self._closed = 0, False
        self._ticket, self._pending = None, None

    @classmethod
    def for_user_trial(cls, controller):
        # Construction performs no IO. The worker validates exact managed paths;
        # this first trial deliberately cannot select or read private members.
        from .goal_trial_resources import TrialResources
        return cls(controller, enabled=True, resources=TrialResources(controller))

    def begin(self, goal="LISTEN_MUSIC"):
        with self._lock:
            if self._ticket is not None:
                self._ticket.cancel.set()
            self._generation += 1
            self._ticket = Ticket(self._generation, uuid.uuid4().hex, goal)
            return self._ticket

    @property
    def unresolved(self):
        """Metadata only: disabling an entry must not discard an admission."""
        with self._lock:
            return self._pending is not None

    def _current(self, ticket):
        with self._lock:
            return not self._closed and ticket is self._ticket and not ticket.cancel.is_set()

    def _check(self, ticket):
        if not self.enabled:
            return AdapterResult("disabled", "explicit_opt_in_required")
        require_worker()
        if not self._current(ticket):
            return AdapterResult("rejected_stale", "adapter_request_superseded")
        if ticket.goal != "LISTEN_MUSIC":
            return AdapterResult("rejected_unavailable", "only_unconstrained_listen_music")
        self.resources.inside(self.c.db_path)
        self.resources.inside(self.c.music_memory.path)
        return None

    def prepare(self, ticket):
        try:
            blocked = self._check(ticket)
            if blocked:
                return blocked
            if not self._gate.acquire(blocking=False):
                return AdapterResult("rejected_unavailable", "adapter_busy")
            try:
                with self._lock:
                    if self._pending is not None:
                        return AdapterResult("needs_verification", "previous_admission_unresolved")
                state = self.c.goal_state()
                # Already-active sessions need no inventory/pref SQL at all.
                evidence = (
                    Resources((), "session_only")
                    if state.media_state in ("playing", "paused", "unknown")
                    else self.resources.capture(state, ticket.cancel, time.monotonic() + 0.3)
                )
                if not self._current(ticket):
                    return AdapterResult("rejected_stale", "cancelled_during_preparation")
                decision = decide(
                    state,
                    self.c.state_store.get("movie", "playing", None),
                    evidence.candidates,
                    now=time.monotonic(),
                    goal=ticket.goal,
                )
                return Prepared(state, evidence, decision, ticket)
            finally:
                self._gate.release()
        except Exception:
            return AdapterResult("needs_verification", "snapshot_unavailable")

    def execute(self, ticket, prepared, *, budget=3):
        admitted = False
        try:
            blocked = self._check(ticket)
            if blocked:
                return blocked
            if not isinstance(prepared, Prepared) or prepared.ticket is not ticket:
                return AdapterResult("rejected_stale", "preparation_owner_mismatch")
            if not 0.05 <= budget <= 5:
                return AdapterResult("rejected_unavailable", "invalid_budget")
            if not self._gate.acquire(blocking=False):
                return AdapterResult("rejected_unavailable", "adapter_busy")
            try:
                if self._pending is not None:
                    return AdapterResult("needs_verification", "previous_admission_unresolved")
                decision, old = prepared.decision, prepared.state
                if decision.plan == "TRY_COLLECTION" and not self.resources.collection_authorized:
                    return AdapterResult("rejected_unavailable", "collection_not_authorized", decision)
                if decision.plan is None:
                    return AdapterResult(decision.status, decision.reason, decision)
                fresh = self.c.goal_state()
                if (
                    replace(fresh, captured=old.captured) != old
                    or not 0 <= time.monotonic() - old.captured <= 1
                ):
                    return AdapterResult("rejected_stale", "state_changed_or_expired", decision)
                if prepared.resources.fingerprint != "session_only":
                    current = self.resources.capture(old, ticket.cancel, time.monotonic() + 0.3)
                    if current.fingerprint != prepared.resources.fingerprint:
                        return AdapterResult("rejected_stale", "resources_changed", decision)
                if not self._current(ticket):
                    return AdapterResult("rejected_stale", "adapter_request_superseded", decision)
                request = GoalRequest(
                    ticket.operation_id,
                    decision.plan,
                    old,
                    time.monotonic() + budget,
                    decision.member,
                    ticket.cancel,
                )
                # From here any unexpected exception is ambiguous, not a safe
                # signal to invoke a second playback operation.
                admitted = True
                with self._lock:
                    self._pending = (ticket, request, decision)
                receipt = self.c.admit_music_goal(request)
                return self._receipt(receipt, decision)
            finally:
                self._gate.release()
        except Exception:
            if admitted:
                return AdapterResult(
                    "unknown_effects",
                    "admission_exception",
                    prepared.decision,
                    GoalReceipt(
                        ticket.operation_id, S.UNKNOWN_EFFECTS, "admission_exception", True
                    ),
                )
            return AdapterResult("needs_verification", "admission_evidence_unavailable")

    def _receipt(self, receipt, decision):
        with self._lock:
            if receipt.status is S.STARTED or (
                receipt.status in (S.REJECTED_STALE, S.REJECTED_UNAVAILABLE, S.FAILED_NO_EFFECT)
                and not receipt.effects_started
            ):
                self._pending = None
        return AdapterResult(receipt.status.value, receipt.reason, decision, receipt)

    def observe(self, ticket):
        require_worker()
        with self._lock:
            pending = self._pending
        if pending is None or pending[0] is not ticket:
            return AdapterResult("rejected_stale", "no_owned_admission")
        try:
            return self._receipt(self.c.observe_music_goal(ticket.operation_id), pending[2])
        except Exception:
            return AdapterResult("unknown_effects", "observation_unavailable", pending[2])

    def validate_prepared(self, ticket, prepared):
        """Recheck the original token; never recapture to revive an old intent."""
        blocked = self._check(ticket)
        if blocked:
            raise SnapshotError(blocked.reason)
        if not isinstance(prepared, Prepared) or prepared.ticket is not ticket:
            raise SnapshotError("preparation_owner_mismatch")
        with self._lock:
            if self._pending is not None:
                raise SnapshotError("previous_admission_unresolved")
        old = prepared.state
        fresh = self.c.goal_state()
        if replace(fresh, captured=old.captured) != old or not 0 <= time.monotonic() - old.captured <= 1:
            raise SnapshotError("state_changed_or_expired")
        current = self.resources.capture(old, ticket.cancel, time.monotonic() + 0.3)
        if current.fingerprint != prepared.resources.fingerprint or current.database_revision != prepared.resources.database_revision:
            raise SnapshotError("resources_changed")

    def run(self, ticket, *, budget=3, allowed_plans=None, prepared=None):
        if prepared is None:
            prepared = self.prepare(ticket)
        if isinstance(prepared, AdapterResult):
            return prepared
        if allowed_plans is not None and prepared.decision.plan is not None and prepared.decision.plan not in allowed_plans:
            return AdapterResult("rejected_unavailable", "plan_not_authorized", prepared.decision)
        result = self.execute(ticket, prepared, budget=budget)
        if result.status != S.ACCEPTED.value:
            return result
        with self._lock:
            deadline = self._pending[1].deadline
        while result.status == S.ACCEPTED.value and time.monotonic() < deadline:
            ticket.cancel.wait(0.02)
            result = self.observe(ticket)
        return self.observe(ticket) if result.status == S.ACCEPTED.value else result

    def close(self):
        with self._lock:
            self._closed = True
            if self._ticket:
                self._ticket.cancel.set()
