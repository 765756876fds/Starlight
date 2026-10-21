"""PlayerRepository - SQLite 存储玩家身份"""
import os
import sqlite3
import threading

from app.core.path_resolve import PROJECT_ROOT

from .models import PlayerIdentity


class PlayerRepository:
    def __init__(self, db_path: str = None):
        # P0-2：默认绝对路径（基于 PROJECT_ROOT），不依赖 CWD
        self.db_path = db_path or str(PROJECT_ROOT / "data" / "player.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    puuid TEXT PRIMARY KEY,
                    game_name TEXT,
                    tag_line TEXT,
                    summoner_id INTEGER,
                    platform TEXT,
                    first_seen REAL,
                    last_seen REAL
                )
            """)
            conn.commit()
            conn.close()

    def upsert(self, identity: PlayerIdentity) -> None:
        if not identity.puuid:
            return
        import time
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO players (puuid, game_name, tag_line, summoner_id, platform, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(puuid) DO UPDATE SET
                    game_name=excluded.game_name,
                    tag_line=excluded.tag_line,
                    summoner_id=excluded.summoner_id,
                    platform=excluded.platform,
                    last_seen=excluded.last_seen
            """, (identity.puuid, identity.game_name, identity.tag_line,
                  identity.summoner_id, identity.platform, now, now))
            conn.commit()
            conn.close()

    def get(self, puuid: str) -> PlayerIdentity | None:
        if not puuid:
            return None
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT puuid, game_name, tag_line, summoner_id, platform FROM players WHERE puuid=?",
                (puuid,)
            ).fetchone()
            conn.close()
        if row:
            return PlayerIdentity(puuid=row[0], game_name=row[1] or "", tag_line=row[2] or "",
                                  summoner_id=row[3] or 0, platform=row[4] or "")
        return None

    def list_all(self) -> list[PlayerIdentity]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT puuid, game_name, tag_line, summoner_id, platform FROM players"
            ).fetchall()
            conn.close()
        return [PlayerIdentity(puuid=r[0], game_name=r[1] or "", tag_line=r[2] or "",
                               summoner_id=r[3] or 0, platform=r[4] or "") for r in rows]
