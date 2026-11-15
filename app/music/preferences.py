"""Small, optional local memory. Never migrates or edits the music/history DB."""
import hashlib
import json
import logging
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from pathlib import Path

log = logging.getLogger(__name__)


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def media_key(song):
    """Source identity wins; missing version/duration must not merge across providers."""
    source = song.get("source_name") or song.get("source_type") or "local"
    identity = song.get("track_id") or song.get("music_id") or song.get("id")
    parts = [source, identity, song.get("title"), song.get("artist"),
             song.get("album"), song.get("duration")]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, default=str).encode()).hexdigest()


class PreferenceStore:
    """Short connections avoid a background service and close/worker lifetime races."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.available = True
        self._initialized = False

    def _run(self, read):
        with self._lock:
            if not self.available:
                return None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(str(self.path), timeout=0.15)) as db, db:
                    if not self._initialized:
                        version = db.execute("PRAGMA user_version").fetchone()[0]
                        if version > 1:
                            raise sqlite3.DatabaseError("newer music memory schema")
                        db.executescript("""
                        CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT);
                        CREATE TABLE IF NOT EXISTS preferences (
                            domain TEXT, kind TEXT, value TEXT, weight REAL,
                            PRIMARY KEY(domain, kind, value));
                        CREATE TABLE IF NOT EXISTS behaviors (
                            id INTEGER PRIMARY KEY, domain TEXT, media_key TEXT,
                            artist TEXT, origin TEXT, occurred REAL);
                        CREATE TABLE IF NOT EXISTS catalog_cache (
                            cache_key TEXT PRIMARY KEY, payload TEXT, updated REAL);
                        PRAGMA user_version=1;
                        """)
                        self._initialized = True
                    return read(db)
            except (sqlite3.Error, OSError):
                self.available = False
                log.exception("music_memory_unavailable; existing history remains untouched")
                return None

    def record(self, song, origin="manual"):
        if not song.get("title"):
            return
        def write(db):
            db.execute("INSERT INTO behaviors(domain,media_key,artist,origin,occurred) VALUES(?,?,?,?,?)",
                       ("music", media_key(song), normalize(song.get("artist")), origin, time.time()))
            db.execute("DELETE FROM behaviors WHERE id NOT IN (SELECT id FROM behaviors ORDER BY id DESC LIMIT 2000)")
        self._run(write)

    def import_history_once(self, rows):
        """Unknown historical intent is a weak, one-time seed, never a rolling auto-play feed."""
        def write(db):
            if db.execute("SELECT 1 FROM memory_meta WHERE key='history_seed_v1'").fetchone():
                return
            for row in list(rows)[:100]:
                db.execute("INSERT INTO behaviors(domain,media_key,artist,origin,occurred) VALUES(?,?,?,?,?)",
                           ("music", media_key(row), normalize(row.get("artist")), "legacy", time.time()))
            db.execute("INSERT INTO memory_meta VALUES('history_seed_v1','1')")
        self._run(write)

    def set_preference(self, kind, value, weight):
        if kind not in {"artist", "track", "style", "language", "era"} or not normalize(value):
            return False
        def write(db):
            db.execute("INSERT OR REPLACE INTO preferences VALUES('music',?,?,?)",
                       (kind, normalize(value), 1 if weight > 0 else -1))
            return True
        return self._run(write) is True

    def profile(self):
        def read(db):
            explicit = {(kind, value): weight for kind, value, weight in db.execute(
                "SELECT kind,value,weight FROM preferences WHERE domain='music'")}
            # Distinct days/media cap repeated manual plays. Recommended/automatic plays have ZERO influence.
            inferred = dict(db.execute("""
                SELECT artist, MIN(5, SUM(weight)) FROM (
                    SELECT artist,media_key,CAST(occurred/86400 AS INT),
                        MAX(CASE origin WHEN 'manual' THEN 1.0 ELSE 0.15 END) weight
                    FROM behaviors WHERE domain='music' AND origin IN ('manual','legacy')
                    AND artist<>'' GROUP BY artist,media_key,CAST(occurred/86400 AS INT)
                ) GROUP BY artist
            """))
            return {"explicit": explicit, "artists": inferred}
        result = self._run(read) or {"explicit": {}, "artists": {}}
        provider = getattr(self, "weak_provider", None)
        if callable(provider):
            result["weak_tracks"] = provider()
        return result

    def cache(self, key, value=None, ttl=300):
        def access(db):
            if value is not None:
                db.execute("INSERT OR REPLACE INTO catalog_cache VALUES(?,?,?)",
                           (key, json.dumps(value, ensure_ascii=False), time.time()))
                return value
            row = db.execute("SELECT payload,updated FROM catalog_cache WHERE cache_key=?", (key,)).fetchone()
            if row and time.time() - row[1] < ttl:
                return json.loads(row[0])
            return None
        return self._run(access)
