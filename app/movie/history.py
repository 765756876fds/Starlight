"""Persistent movie playback history (online and local media)."""
from __future__ import annotations

import os
import sqlite3
import threading
import time


class MovieHistory:
    """Small, independent SQLite store for movie history.

    It deliberately does not share the music history table: movie rows have
    different replay semantics and local paths must be retained.
    """

    MAX_ITEMS = 50

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(os.path.expanduser(str(db_path)))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS movie_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                media_type TEXT NOT NULL,
                replay_value TEXT NOT NULL,
                source_name TEXT DEFAULT '',
                cover_url TEXT DEFAULT '',
                played_at REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_movie_history_played_at "
                         "ON movie_history(played_at DESC)")

    def record(self, *, media_key: str, title: str, media_type: str,
               replay_value: str, source_name: str = "",
               cover_url: str = "", played_at: float | None = None) -> int:
        now = float(time.time() if played_at is None else played_at)
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO movie_history
                (media_key, title, media_type, replay_value, source_name, cover_url, played_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_key) DO UPDATE SET
                    title=excluded.title, media_type=excluded.media_type,
                    replay_value=excluded.replay_value, source_name=excluded.source_name,
                    cover_url=excluded.cover_url, played_at=excluded.played_at""",
                         (media_key, title, media_type, replay_value, source_name,
                          cover_url, now))
            conn.execute("""DELETE FROM movie_history WHERE id IN (
                SELECT id FROM movie_history ORDER BY played_at DESC
                LIMIT -1 OFFSET ?)""", (self.MAX_ITEMS,))
            row = conn.execute("SELECT id FROM movie_history WHERE media_key=?",
                               (media_key,)).fetchone()
            return int(row[0])

    def list(self, limit: int = MAX_ITEMS) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM movie_history ORDER BY played_at DESC LIMIT ?",
                                (max(0, int(limit)),)).fetchall()
            return [dict(row) for row in rows]

    def delete(self, row_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM movie_history WHERE id=?", (int(row_id),))
            return cur.rowcount > 0

