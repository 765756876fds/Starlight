"""Optional local film memory. Existing movie/music databases remain untouched."""
from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

from .library import title_key
from .cms_parser import episode_eps

log = logging.getLogger(__name__)
KINDS = {"movie", "tv", "anime", "variety"}
RESUME_FIELDS = ("vod_name", "vod_id", "first_url", "play_url", "local_file",
                 "source_name", "vod_pic", "vod_year", "vod_area", "type_name", "vod_class")


def work_key(work):
    sources = work.get("sources") or []
    identity = [(s.get("platform"), s.get("source_id")) for s in sources]
    value = [title_key(work.get("title", "")), work.get("kind"), work.get("year"),
             work.get("season"), work.get("region"), sorted(identity, key=str)]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def safe_work(work):
    return {key: work.get(key) for key in ("title", "kind", "year", "season", "region", "genres",
            "sources", "score", "score_source", "cover_url") if work.get(key) is not None}


def work_tags(work):
    values = [("genre", v) for v in work.get("genres", []) if isinstance(v, str)]
    if work.get("region"):
        values.append(("region", work["region"]))
    if type(work.get("year")) is int:
        values.append(("era", str(work["year"] // 10 * 10)))
    values.extend(("platform", s["platform"]) for s in work.get("sources", []) if s.get("platform"))
    return values


class FilmMemory:
    def __init__(self, path):
        self.path = Path(path)
        self.available = True
        self._lock = threading.RLock()
        self._initialized = False

    def _run(self, action, default=None):
        with self._lock:
            if not self.available:
                return default
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=.15)) as db, db:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version > 1:
                        raise sqlite3.DatabaseError("newer film memory schema")
                    if not self._initialized or version == 0:
                        db.executescript("""
                        CREATE TABLE IF NOT EXISTS preferences (
                            domain TEXT, kind TEXT, value TEXT, weight INTEGER,
                            PRIMARY KEY(domain,kind,value));
                        CREATE TABLE IF NOT EXISTS viewing (
                            content_key TEXT PRIMARY KEY, domain TEXT, work TEXT, item TEXT,
                            position REAL, duration REAL, episode INTEGER, state TEXT,
                            meaningful INTEGER, updated REAL);
                        CREATE TABLE IF NOT EXISTS feedback (
                            content_key TEXT PRIMARY KEY, domain TEXT, work TEXT,
                            positive INTEGER, updated REAL);
                        CREATE TABLE IF NOT EXISTS actions (
                            id INTEGER PRIMARY KEY, domain TEXT, action TEXT,
                            title TEXT, content_key TEXT, created REAL);
                        PRAGMA user_version=1;
                        """)
                        self._initialized = True
                    return action(db)
            except (OSError, sqlite3.Error, ValueError, TypeError):
                self.available = False
                log.exception("film_memory_unavailable; original history preserved")
                return default

    def set_preference(self, domain, kind, value, positive=True):
        return self.set_preferences(domain, [(kind, value, positive)])

    def set_preferences(self, domain, entries):
        if domain not in KINDS or not entries or any(kind not in {"content", "genre", "region", "era", "platform"}
                                                    or not value for kind, value, _ in entries):
            return False
        def write(db):
            db.executemany("INSERT OR REPLACE INTO preferences VALUES(?,?,?,?)",
                           [(domain, kind, str(value), 1 if positive else -1) for kind, value, positive in entries])
            return True
        return self._run(write, False)

    def observe(self, work, item, position, duration, episode=0, *, ended=False):
        if work.get("kind") not in KINDS or not work.get("title"):
            return False
        if type(position) not in (int, float) or type(duration) not in (int, float):
            return False
        if not 0 <= position < 10**7 or not 0 < duration < 10**7:
            return False  # Unknown duration must not turn an opened detail into a watch.
        saved_item = {k: item[k] for k in RESUME_FIELDS if k in item and isinstance(item[k], (str, bool, int))}
        # A private local replay payload, never headers, credentials, raw API blobs or recordings.
        if len(json.dumps(saved_item)) > 512_000:
            saved_item = {}
        completed = bool(ended or position / duration >= .95)
        meaningful = int(position >= 30 or (duration <= 60 and position / duration >= .8))
        state = "completed" if completed else "continuing" if position >= 5 else "opened"
        if ended and work["kind"] in ("tv", "anime") and int(episode or 0) + 1 < len(episode_eps(item)):
            episode, position, state, meaningful = int(episode or 0) + 1, 0, "continuing", 1
        key = work_key(work)
        def write(db):
            db.execute("INSERT OR REPLACE INTO viewing VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (key, work["kind"], json.dumps(safe_work(work), ensure_ascii=False),
                        json.dumps(saved_item, ensure_ascii=False), position, duration,
                        int(episode or 0), state, meaningful, time.time()))
            db.execute("DELETE FROM viewing WHERE content_key NOT IN "
                       "(SELECT content_key FROM viewing ORDER BY updated DESC LIMIT 500)")
            return True
        return self._run(write, False)

    def remember(self, work, positive):
        domain = work.get("kind")
        if domain not in KINDS or not work.get("title"):
            return False
        key = work_key(work)
        def write(db):
            db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?,?,?)",
                       (domain, "content", key, 1 if positive else -1))
            db.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?)",
                       (key, domain, json.dumps(safe_work(work), ensure_ascii=False), bool(positive), time.time()))
            db.execute("DELETE FROM feedback WHERE content_key NOT IN "
                       "(SELECT content_key FROM feedback ORDER BY updated DESC LIMIT 500)")
            return True
        return self._run(write, False)

    @staticmethod
    def _records(db, domain):
        rows = db.execute("SELECT content_key,work,item,position,duration,episode,state,meaningful,updated "
                          "FROM viewing WHERE domain=? ORDER BY updated DESC", (domain,)).fetchall()
        return [dict(key=r[0], work=json.loads(r[1]), item=json.loads(r[2]), position=r[3],
                     duration=r[4], episode=r[5], state=r[6], meaningful=bool(r[7]), updated=r[8]) for r in rows]

    def records(self, domain):
        return self._run(lambda db: self._records(db, domain), [])

    def record_action(self, action, title, *, work=None):
        if action not in ("search", "select") or not isinstance(title, str) or not title.strip() or "://" in title:
            return False
        domain = work.get("kind") if isinstance(work, dict) else "unknown"
        domain = domain if domain in KINDS else "unknown"
        def write(db):
            db.execute("INSERT INTO actions(domain,action,title,content_key,created) VALUES(?,?,?,?,?)",
                       (domain, action, title.strip()[:160], work_key(work) if work else "", time.time()))
            db.execute("DELETE FROM actions WHERE id NOT IN (SELECT id FROM actions ORDER BY id DESC LIMIT 200)")
            return True
        return self._run(write, False)

    @staticmethod
    def _profile(explicit, records, feedback):
        inferred = {}
        for row in records:
            if not row["meaningful"]:
                continue
            work = row["work"]
            for tag in set(work_tags(work)):
                inferred[tag] = min(3, inferred.get(tag, 0) + .5)
        for entry in feedback:
            if not entry["positive"]:
                continue
            work = entry["work"]
            for tag in set(work_tags(work)):
                inferred[tag] = min(6, inferred.get(tag, 0) + 2)
        return {"explicit": explicit, "inferred": inferred}

    @staticmethod
    def _feedback(db, domain):
        return [dict(key=row[0], work=json.loads(row[1]), positive=bool(row[2])) for row in db.execute(
            "SELECT content_key,work,positive FROM feedback WHERE domain=? ORDER BY updated DESC", (domain,))]

    def snapshot(self, domain):
        def read(db):
            db.execute("BEGIN")
            explicit = {(k, v): w for k, v, w in db.execute(
                "SELECT kind,value,weight FROM preferences WHERE domain=?", (domain,))}
            records, feedback = self._records(db, domain), self._feedback(db, domain)
            return dict(profile=self._profile(explicit, records, feedback), records=records, feedback=feedback)
        return self._run(read, dict(profile={"explicit": {}, "inferred": {}}, records=[], feedback=[]))

    def profile(self, domain):
        return self.snapshot(domain)["profile"]

    def feedback_records(self, domain):
        return self._run(lambda db: self._feedback(db, domain), [])
