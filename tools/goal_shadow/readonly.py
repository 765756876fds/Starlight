"""Read existing schemas without constructors, migrations, caches or write APIs."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
import unicodedata

from .contracts import Availability as A, CapabilityEvidence, Resource, WorldStateSnapshot

LIMIT = 64


def opaque(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:24]


def song_identity(song):
    if not isinstance(song, dict):
        return ""
    if song.get("source_name") and song.get("track_id"):
        return opaque(("online", str(song["source_name"]), str(song["track_id"])))
    key = song.get("music_id") or song.get("id")
    if type(key) is int and key > 0:
        return opaque(("local", key))
    return opaque(("path", song["path"])) if song.get("path") else ""


def artist_ref(value):
    return opaque(" ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())) if value else ""


def evidence(state, source, now, reason="", *, ttl=2, reliable=True):
    return CapabilityEvidence(state, source, now, reliable, ttl, reason)


def off_gui_thread():
    qt = sys.modules.get("PySide6.QtCore")
    if qt is not None:
        app = qt.QCoreApplication.instance()
        if app is not None and qt.QThread.currentThread() == app.thread():
            raise RuntimeError("read_snapshot_requires_worker")


@contextmanager
def immutable_database(path, deadline):
    """No WAL/SHM creation. Refuse active journals rather than hide live WAL data.

    immutable=1 is used ONLY for a quiescent file; metadata is checked after
    the read. This is not a replacement for a live DB owner's snapshot API.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError("database_missing")
    def stamp():
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise RuntimeError("active_journal_requires_owner_snapshot")
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    before = stamp()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=.02)
    try:
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
        connection.set_authorizer(lambda op, a, b, *_: sqlite3.SQLITE_OK if op in allowed or
                                  op == sqlite3.SQLITE_PRAGMA and a == "user_version" and b is None
                                  else sqlite3.SQLITE_DENY)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        yield connection
        if stamp() != before:
            raise RuntimeError("database_changed_during_snapshot")
    finally:
        connection.close()


def collections(path, *, now, deadline):
    """Bounded metadata evidence. Existing membership is NOT playback proof."""
    if path is None:
        return (), evidence(A.UNKNOWN, "music_db", now, "database_location_unknown")
    try:
        with immutable_database(path, deadline) as db:
            groups = db.execute("SELECT id,name FROM playlists ORDER BY id LIMIT ?", (LIMIT+1,)).fetchall()
            truncated = len(groups) > LIMIT
            rows = []
            for pid, name in groups[:LIMIT]:
                songs = db.execute("SELECT id,music_id,source_name,track_id,artist FROM playlist_songs "
                                   "WHERE playlist_id=? ORDER BY id LIMIT ?", (pid, LIMIT+1)).fetchall()
                truncated |= len(songs) > LIMIT
                for sid, mid, source, track, artist in songs[:LIMIT]:
                    content = song_identity(dict(music_id=mid, source_name=source, track_id=track))
                    rows.append(Resource(
                        opaque((pid, sid)), "collection", evidence(A.YES, "playlist_membership", now),
                        evidence(A.UNKNOWN, "music_db", now, "membership_is_not_decoded_audio"),
                        evidence(A.YES, "personal_database", now), count=1,
                        # Fixed liked playlist is an explicit save, not an inferred artist taste.
                        explicit_preference=1 if name == "我喜欢的音乐" else 0,
                        content_ref=content, artist_ref=artist_ref(artist),
                    ))
                    if len(rows) >= LIMIT:
                        truncated = True
                        break
                if len(rows) >= LIMIT:
                    break
        return tuple(rows), evidence(A.UNKNOWN if truncated else A.YES, "music_db", now,
                                     "inventory_truncated" if truncated else "bounded_read_only_memberships")
    except FileNotFoundError:
        return (), evidence(A.NO, "music_db", now, "database_missing")
    except (sqlite3.Error, OSError, RuntimeError):
        return (), evidence(A.UNKNOWN, "music_db", now, "database_unreadable_busy_or_changed")


def preference_summary(path, *, now, deadline):
    """Return counts and opaque artist evidence; never initialize PreferenceStore."""
    if path is None:
        return ("preferences_location_unknown",), {}, {}
    try:
        with immutable_database(path, deadline) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                return ("preferences_future_schema",), {}, {}
            explicit = db.execute("SELECT COUNT(*) FROM preferences WHERE domain='music'").fetchone()[0]
            manual = db.execute("SELECT COUNT(*) FROM behaviors WHERE domain='music' AND origin IN ('manual','legacy')").fetchone()[0]
            explicit_artists = {artist_ref(value): weight for value, weight in db.execute(
                "SELECT value,weight FROM preferences WHERE domain='music' AND kind='artist' LIMIT ?", (LIMIT,))}
            inferred = {artist_ref(artist): weight for artist, weight in db.execute("""
                SELECT artist, MIN(5, SUM(weight)) FROM (
                  SELECT artist,media_key,CAST(occurred/86400 AS INT),
                    MAX(CASE origin WHEN 'manual' THEN 1.0 ELSE 0.15 END) weight
                  FROM behaviors WHERE domain='music' AND origin IN ('manual','legacy')
                  AND artist<>'' GROUP BY artist,media_key,CAST(occurred/86400 AS INT)
                ) GROUP BY artist LIMIT ?""", (LIMIT,))}
        return (f"explicit_preference_count={explicit}", f"reliable_history_count={manual}",
                "artist_evidence_only_other_dimensions_unverified"), explicit_artists, inferred
    except FileNotFoundError:
        return ("preferences_missing",), {}, {}
    except (sqlite3.Error, OSError, RuntimeError):
        return ("preferences_unreadable_busy_or_changed",), {}, {}


def controller_stamp(controller):
    lock = getattr(controller, "_lock", None)
    if lock is None or not lock.acquire(timeout=.02):
        raise TimeoutError("controller_snapshot_busy")
    try:
        return ((id(controller), getattr(controller, "_play_request_id", None)),
                dict(getattr(controller, "_current_song", None) or {}),
                getattr(controller, "_is_switching_track", None))
    finally:
        lock.release()


def queue_stamp(controller, now):
    queue = getattr(controller, "queue", None)
    lock = getattr(queue, "_lock", None)
    if lock is None or not lock.acquire(timeout=.02):
        return None, evidence(A.UNKNOWN, "PlayQueue", now, "queue_busy_or_missing")
    try:
        items = getattr(queue, "_queue", None)
        index, revision = getattr(queue, "_current_index", None), getattr(queue, "_revision", None)
        if not isinstance(items, list) or type(index) is not int or type(revision) is not int:
            return None, evidence(A.UNKNOWN, "PlayQueue", now, "queue_contract_unknown")
        current = song_identity(items[index]) if 0 <= index < len(items) else ""
        return (revision, len(items), index, current), evidence(A.YES, "PlayQueue", now, "atomic_counters_only")
    finally:
        lock.release()


def cached_charts(controller, now):
    service = getattr(controller, "recommendations", None)
    lock = getattr(service, "_lock", None)
    if lock is None or not lock.acquire(timeout=.02):
        return (), evidence(A.UNKNOWN, "recommendation_cache", now, "cache_busy_or_missing")
    try:
        catalog, at = getattr(service, "_catalog", None), getattr(service, "_catalog_at", 0)
        if not catalog:
            return (), evidence(A.NO, "recommendation_cache", now, "no_existing_catalog_cache")
        if not isinstance(catalog, tuple) or len(catalog) != 2 or not isinstance(catalog[1], dict):
            return (), evidence(A.UNKNOWN, "recommendation_cache", now, "cache_shape_unknown")
        manager = getattr(controller, "online_manager", None)
        life = getattr(manager, "_life_lock", None)
        if life is None or not life.acquire(timeout=.02):
            return (), evidence(A.UNKNOWN, "recommendation_cache", now, "source_revision_unproven")
        try:
            revision = tuple(sorted(getattr(manager, "_source_versions", {}).items()))
            current = catalog[0] == (id(manager), revision) and 0 <= now - at <= 120
        finally:
            life.release()
        if not current:
            return (), evidence(A.UNKNOWN, "recommendation_cache", now, "cache_expired_or_source_changed")
        rows = []
        for source, groups in list(catalog[1].items())[:LIMIT]:
            for group in list(groups or [])[:LIMIT]:
                for chart in list(group.get("lists", []))[:LIMIT]:
                    if chart.get("id"):
                        rows.append(Resource(opaque((source, chart["id"])), "chart",
                            evidence(A.YES, "existing_catalog_metadata", at, ttl=120),
                            evidence(A.UNKNOWN, "existing_catalog_metadata", now, "chart_metadata_is_not_play_url"),
                            evidence(A.UNKNOWN, "not_a_personal_collection", now)))
                    if len(rows) >= LIMIT:
                        return tuple(rows), evidence(A.YES, "recommendation_cache", at, ttl=120)
        return tuple(rows), evidence(A.YES, "recommendation_cache", at, ttl=120)
    except (TypeError, AttributeError, ValueError):
        return (), evidence(A.UNKNOWN, "recommendation_cache", now, "cache_contract_changed")
    finally:
        lock.release()


class LiveReadAdapter:
    """An explicitly supplied controller and separate read port; no auto discovery.

    No production hook calls this. All private field reads are contained here;
    missing/changed contracts return unknown, never construct production objects.
    """
    def __init__(self, controller, port, *, movie_reader=None, db_path=None, preference_path=None):
        self.controller, self.port, self.movie_reader = controller, port, movie_reader
        self.db_path = db_path if db_path is not None else getattr(controller, "db_path", None)
        memory = getattr(controller, "music_memory", None)
        self.preference_path = preference_path if preference_path is not None else getattr(memory, "path", None)

    def generation(self):
        return controller_stamp(self.controller)[0]

    def capture(self, budget=.6):
        off_gui_thread()
        started = time.monotonic()
        deadline = started + min(max(budget, .05), 1)
        now = started
        unknown = evidence(A.UNKNOWN, "mpv_read_port", now, "state_unproven")
        generation, song, switching = controller_stamp(self.controller)
        before_queue, queue_proof = queue_stamp(self.controller, now)
        state, session, music, movie = "unknown", unknown, unknown, "unknown"
        if switching is False:
            props = self.port.sample(deadline)
            path = props.get("path")
            position = props.get("time-pos")
            valid_position = type(position) in (int, float) and math.isfinite(position) and position >= 0
            if props.get("idle-active") is True:
                state = "stopped"
                music = evidence(A.YES, "mpv_idle_observation", now)
                session = evidence(A.NO, "mpv_idle_observation", now)
            elif (props.get("idle-active") is False and isinstance(path, str) and path and
                  path == song.get("path") and valid_position and type(props.get("pause")) is bool):
                session = evidence(A.YES, "mpv_path_and_controller_identity", now)
                if props["pause"] is True:
                    state = "paused"
                elif type(props.get("time-pos-after")) in (int, float) and props["time-pos-after"] > position:
                    state = "playing"
                if state != "unknown":
                    music = evidence(A.YES, "independent_mpv_property_observation", now)
        if self.movie_reader is not None and time.monotonic() < deadline:
            try:
                observed = self.movie_reader()
                movie = getattr(observed, "state", "unknown")
                if movie not in ("playing", "paused", "stopped", "unknown"):
                    movie = "unknown"
            except Exception:
                movie = "unknown"
        resources, inventory = collections(self.db_path, now=now, deadline=deadline)
        notes, explicit, history = preference_summary(self.preference_path, now=now, deadline=deadline)
        resources = tuple(replace(r,
            explicit_preference=explicit.get(r.artist_ref, r.explicit_preference),
            historical_preference=history.get(r.artist_ref, 0)) for r in resources)
        charts, catalog = cached_charts(self.controller, now)
        after_generation, after_song, after_switching = controller_stamp(self.controller)
        after_queue, _ = queue_stamp(self.controller, now)
        # A matching current decoded item is the only existing playback proof;
        # never upgrade arbitrary stored membership or a cached chart URL.
        if state in ("playing", "paused") and session.state is A.YES:
            resources = tuple(replace(r, playable=session) if r.content_ref == song_identity(song) else r
                              for r in resources)
        consistent = (after_generation == generation and song_identity(after_song) == song_identity(song)
                      and song.get("path") == after_song.get("path") and switching is after_switching
                      and before_queue == after_queue and generation[1] is not None
                      and time.monotonic() < deadline)
        if state in ("playing", "paused") and before_queue and before_queue[3] and before_queue[3] != song_identity(song):
            queue_proof = evidence(A.UNKNOWN, "PlayQueue", now, "queue_current_does_not_match_decoder")
        return WorldStateSnapshot(now, time.monotonic(), generation, state, music, session,
            song_identity(song), queue_proof, before_queue[0] if before_queue else None,
            resources + charts, movie, consistent, notes, inventory, catalog)
