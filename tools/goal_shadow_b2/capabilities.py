"""Read-only, bounded attempt evidence. No URL lookup, player or preference write."""
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import time

from tools.goal_shadow.contracts import Availability as A
from tools.goal_shadow.readonly import opaque, off_gui_thread


class Level(str, Enum):
    ATTEMPTABLE = "ATTEMPTABLE"
    PLAYBACK_CONFIRMED = "PLAYBACK_CONFIRMED"
    UNVERIFIED = "UNVERIFIED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class Capability:
    ref: str
    provider: str
    level: Level
    observed_at: float
    epoch: tuple = ()
    exists: A = A.UNKNOWN
    owned: A = A.UNKNOWN
    content_ref: str = ""
    basis: tuple[str, ...] = ()
    preparation_required: bool = False
    explicit_preference: float = 0
    history: float = 0
    ttl: float = 2

    def fresh(self, now):
        return 0 <= now-self.observed_at <= self.ttl


def capability_current(old, refreshed, now):
    """Pure check after a fresh provider read, not a promise to cancel an action."""
    return (old.ref==refreshed.ref and old.epoch==refreshed.epoch and
            old.content_ref==refreshed.content_ref and refreshed.fresh(now) and
            refreshed.level in (Level.ATTEMPTABLE,Level.PLAYBACK_CONFIRMED) and
            refreshed.exists is A.YES and (refreshed.provider!="collection" or refreshed.owned is A.YES))


@dataclass(frozen=True)
class SourceCapability:
    key: str = field(repr=False)
    registered: bool | None = None
    resolve: bool = False
    detail: bool = False
    epoch: tuple = ()
    observed_at: float = 0


def source_capabilities(manager, now):
    """Exact registered source names only. Never call accepts/search/get_play_url.

    Plugin-name aliases that need an accepts() call deliberately remain unknown.
    """
    lock = getattr(manager,"_life_lock",None)
    if lock is None or not lock.acquire(timeout=.02):
        return ()
    try:
        shutdown = getattr(getattr(manager,"_executor",None),"_shutdown",None)
        registered = True if shutdown is False else False if shutdown is True else None
        sources = getattr(manager,"_sources",{})
        versions = getattr(manager,"_source_versions",{})
        rows = []
        for name, source in list(sources.items())[:64]:
            resolve = getattr(type(source),"get_play_url",None)
            detail = getattr(type(source),"get_top_list_detail",None)
            rows.append(SourceCapability(name,registered,
                callable(resolve) and not getattr(resolve,"__isabstractmethod__",False),
                callable(detail), (id(manager),name,versions.get(name)),now))
        return tuple(rows)
    finally:
        lock.release()


def local_attempt(path, *, roots, extensions):
    """File existence/access/extension justify a TRY, never decoder success."""
    off_gui_thread()
    try:
        target = Path(path).resolve()
        if not any(target.is_relative_to(Path(root).resolve()) for root in roots):
            return Level.UNVERIFIED,("outside_explicit_lab_roots",)
        if not target.is_file():
            return Level.UNAVAILABLE,("file_missing",)
        before = target.stat()
        with target.open("rb") as stream:
            nonempty = bool(stream.read(1))
        after = target.stat()
        if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
            return Level.UNVERIFIED,("file_changed",)
        if not nonempty:
            return Level.UNAVAILABLE,("file_empty",)
        if target.suffix.casefold() not in extensions:
            return Level.UNVERIFIED,("format_support_unproven",)
        return Level.ATTEMPTABLE,("file_exists","readable_nonempty","configured_extension","decode_not_proven")
    except FileNotFoundError:
        return Level.UNAVAILABLE,("file_missing",)
    except PermissionError:
        return Level.UNAVAILABLE,("file_not_readable",)
    except OSError:
        return Level.UNVERIFIED,("file_query_failed",)


class PersonalCollectionProvider:
    def propose(self, snapshot, *, sources=(), roots=(), extensions=frozenset({".wav"}), now=None):
        now = time.monotonic() if now is None else now
        if snapshot.status not in ("ok","partial"):
            return ()
        rows = []
        for member in snapshot.rows:
            level, basis = Level.UNVERIFIED,("membership_only",)
            epoch = snapshot.file_identity + ((snapshot.owner_epoch,) if snapshot.owner_epoch is not None else ())
            if not member.owned:
                basis = ("ownership_unproven",)
            elif member.kind == "local" and member.path:
                level,basis = local_attempt(member.path,roots=roots,extensions=extensions)
            elif member.kind == "online" and member.source and member.track_id:
                source = next((s for s in sources if s.key==member.source),None)
                if source and 0 <= now-source.observed_at <= 2:
                    epoch += source.epoch
                    if source.registered is False:
                        level,basis = Level.UNAVAILABLE,("registered_source_closed",)
                    elif source.registered is True and source.resolve:
                        level,basis = Level.ATTEMPTABLE,("stable_source_track_identity","registered_resolver","url_not_resolved","decode_not_proven")
            rows.append(Capability(member.ref,"collection",level,now,epoch,A.YES,
                A.YES if member.owned else A.UNKNOWN,member.content_ref,basis,
                preparation_required=member.kind=="online",explicit_preference=int(member.explicit_saved)))
        return tuple(rows)


@dataclass(frozen=True)
class ChartRecord:
    ref: str
    source: str = field(repr=False)
    observed_at: float = 0
    concrete_tracks: int = 0


def recommendation_contract(service):
    """Inspect the audited existing bounds; never call play()/choices().

    The 45s wall deadline remains a documented implementation fact, not an
    enforceable guarantee inferred from method presence.
    """
    return (type(service).__module__=="app.music.recommendation" and
            type(service).__name__=="RecommendationService" and
            getattr(service,"_closed",True) is False and getattr(service,"_busy",True) is False and
            getattr(service,"MAX_CHARTS",None)==3 and getattr(service,"MAX_TRIES",None)==4 and
            getattr(service,"READ_TIMEOUT",None)==20 and getattr(service,"MAX_PENDING_READS",None)==2)


class ChartProvider:
    def propose(self, charts, *, sources=(), service=None, now=None):
        now = time.monotonic() if now is None else now
        rows = []
        for chart in charts[:64]:
            source = next((s for s in sources if s.key==chart.source),None)
            level,basis = Level.UNVERIFIED,("catalog_metadata_only",)
            epoch = source.epoch if source else ()
            if (source and 0 <= now-source.observed_at <= 2 and 0 <= now-chart.observed_at <= 120 and
                    source.registered is True and source.resolve and source.detail and recommendation_contract(service)):
                level = Level.ATTEMPTABLE
                basis = ("fresh_catalog","registered_detail_and_resolver","existing_bounded_recommendation_plan", "decode_not_proven")
            rows.append(Capability(chart.ref,"chart",level,now,epoch,A.YES,A.UNKNOWN,
                                   opaque((chart.ref,"candidate_preparation")),basis,True))
        if not rows:
            return ()
        # RecommendationService.play does not take an exact chart ID. Propose
        # ONE existing business macro, not a second per-chart fallback engine.
        proven = [r for r in rows if r.level is Level.ATTEMPTABLE]
        chosen = (proven or rows)[0]
        from dataclasses import replace
        return (replace(chosen,ref=opaque(("existing_recommendation",id(service))),
                        content_ref="candidate_requires_existing_service_preparation",
                        basis=chosen.basis+("existing_service_owns_song_source_fallback",)),)
