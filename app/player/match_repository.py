"""MatchRepository - 本地对局历史 SQLite 存储"""
import os
import sqlite3
import threading
import time
from typing import Optional, Dict, Any, List


class MatchRepository:
    def __init__(self, db_path: str = "data/player.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS match_records (
                    local_match_id TEXT PRIMARY KEY,
                    start_time REAL,
                    end_time REAL,
                    duration REAL,
                    result TEXT,
                    game_mode TEXT,
                    map_name TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS match_players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    local_match_id TEXT NOT NULL,
                    puuid TEXT,
                    riot_id TEXT,
                    team TEXT,
                    champion_name TEXT,
                    champion_id INTEGER,
                    position TEXT,
                    kills INTEGER,
                    deaths INTEGER,
                    assists INTEGER,
                    gold REAL,
                    is_self INTEGER DEFAULT 0,
                    UNIQUE(local_match_id, riot_id),
                    FOREIGN KEY(local_match_id) REFERENCES match_records(local_match_id)
                )
            """)
            # Migrate: add puuid column if missing (existing DBs)
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(match_players)").fetchall()]
                if "puuid" not in cols:
                    conn.execute("ALTER TABLE match_players ADD COLUMN puuid TEXT")
            except Exception:
                pass
            conn.commit()
            conn.close()

    def create_match(self, local_match_id: str, start_time: float,
                     game_mode: str, map_name: str):
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                conn.execute("""
                    INSERT OR IGNORE INTO match_records
                    (local_match_id, start_time, game_mode, map_name)
                    VALUES (?, ?, ?, ?)
                """, (local_match_id, start_time, game_mode, map_name))
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[MatchRepo] create_match 失败: {e}")

    def finish_match(self, local_match_id: str, end_time: float,
                     duration: float, result: str):
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                conn.execute("""
                    UPDATE match_records
                    SET end_time=?, duration=?, result=?
                    WHERE local_match_id=?
                """, (end_time, duration, result, local_match_id))
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[MatchRepo] finish_match 失败: {e}")

    def save_match_players(self, local_match_id: str, players: List[Dict[str, Any]]):
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                for p in players:
                    conn.execute("""
                        INSERT OR IGNORE INTO match_players
                        (local_match_id, puuid, riot_id, team, champion_name, champion_id,
                         position, kills, deaths, assists, gold, is_self)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        local_match_id,
                        p.get("puuid"),
                        p.get("riot_id", ""),
                        p.get("team", ""),
                        p.get("champion_name", ""),
                        p.get("champion_id", 0),
                        p.get("position", ""),
                        p.get("kills", 0),
                        p.get("deaths", 0),
                        p.get("assists", 0),
                        p.get("gold", 0.0),
                        1 if p.get("is_self") else 0,
                    ))
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[MatchRepo] save_match_players 失败: {e}")

    def get_match(self, local_match_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM match_records WHERE local_match_id=?",
                (local_match_id,)
            ).fetchone()
            players = conn.execute(
                "SELECT * FROM match_players WHERE local_match_id=?",
                (local_match_id,)
            ).fetchall()
            conn.close()
        if row:
            return {
                "match": dict(row),
                "players": [dict(p) for p in players],
            }
        return None

    def count_matches(self) -> int:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            n = conn.execute("SELECT COUNT(*) FROM match_records").fetchone()[0]
            conn.close()
        return n
