"""HistoryRepository - 只读查询本地比赛历史"""
import sqlite3
import threading
from typing import Optional, List, Dict, Any


class HistoryRepository:
    """只读查询层：不修改 match_records / match_players / players"""

    def __init__(self, db_path: str = "data/player.db"):
        self.db_path = db_path
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # 1. get_match
    def get_match(self, local_match_id: str) -> Optional[Dict[str, Any]]:
        try:
            with self._lock:
                conn = self._connect()
                row = conn.execute(
                    "SELECT * FROM match_records WHERE local_match_id=?",
                    (local_match_id,)
                ).fetchone()
                players = conn.execute(
                    "SELECT * FROM match_players WHERE local_match_id=? ORDER BY id",
                    (local_match_id,)
                ).fetchall()
                conn.close()
        except sqlite3.OperationalError:
            return None
        if row:
            return {
                "match": dict(row),
                "players": [dict(p) for p in players],
            }
        return None

    # 2. get_match_players
    def get_match_players(self, local_match_id: str) -> List[Dict[str, Any]]:
        try:
            with self._lock:
                conn = self._connect()
                rows = conn.execute(
                    "SELECT * FROM match_players WHERE local_match_id=? ORDER BY id",
                    (local_match_id,)
                ).fetchall()
                conn.close()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    # 3. get_recent_matches
    def get_recent_matches(self, limit: int = 10,
                           include_incomplete: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM match_records"
        if not include_incomplete:
            sql += " WHERE end_time IS NOT NULL"
        sql += " ORDER BY start_time DESC LIMIT ?"
        try:
            with self._lock:
                conn = self._connect()
                rows = conn.execute(sql, (limit,)).fetchall()
                conn.close()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    # 4. get_player_matches
    def get_player_matches(self, puuid: Optional[str] = None,
                           riot_id: Optional[str] = None,
                           limit: int = 20,
                           include_incomplete: bool = False) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if puuid:
            clauses.append("mp.puuid = ?")
            params.append(puuid)
        elif riot_id:
            clauses.append("mp.riot_id = ?")
            params.append(riot_id)
        else:
            return []

        where = " AND ".join(clauses)
        completed = "" if include_incomplete else " AND m.end_time IS NOT NULL"
        sql = f"""
            SELECT DISTINCT m.* FROM match_records m
            JOIN match_players mp ON m.local_match_id = mp.local_match_id
            WHERE {where}{completed}
            ORDER BY m.start_time DESC
            LIMIT ?
        """
        params.append(limit)
        try:
            with self._lock:
                conn = self._connect()
                rows = conn.execute(sql, params).fetchall()
                conn.close()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    # 5. get_champion_stats
    def get_champion_stats(self, puuid: Optional[str] = None,
                           riot_id: Optional[str] = None,
                           limit_matches: int = 20) -> List[Dict[str, Any]]:
        if not puuid and not riot_id:
            return []
        ident_col = "puuid" if puuid else "riot_id"
        ident_val = puuid or riot_id
        sql = f"""
            SELECT mp.champion_name,
                   COUNT(*) AS games,
                   SUM(CASE WHEN m.result='Win' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN m.result='Lose' THEN 1 ELSE 0 END) AS losses,
                   SUM(mp.kills) AS kills,
                   SUM(mp.deaths) AS deaths,
                   SUM(mp.assists) AS assists
            FROM match_players mp
            JOIN match_records m ON mp.local_match_id = m.local_match_id
            WHERE mp.{ident_col} = ? AND m.end_time IS NOT NULL
            GROUP BY mp.champion_name
            ORDER BY games DESC, champion_name
        """
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, (ident_val,)).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    # 6. get_role_stats
    def get_role_stats(self, puuid: Optional[str] = None,
                       riot_id: Optional[str] = None,
                       limit_matches: int = 20) -> List[Dict[str, Any]]:
        if not puuid and not riot_id:
            return []
        ident_col = "puuid" if puuid else "riot_id"
        ident_val = puuid or riot_id
        sql = f"""
            SELECT
                CASE WHEN mp.position IS NULL OR mp.position='' THEN 'UNKNOWN' ELSE mp.position END AS position,
                COUNT(*) AS games,
                SUM(CASE WHEN m.result='Win' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN m.result='Lose' THEN 1 ELSE 0 END) AS losses
            FROM match_players mp
            JOIN match_records m ON mp.local_match_id = m.local_match_id
            WHERE mp.{ident_col} = ? AND m.end_time IS NOT NULL
            GROUP BY position
            ORDER BY games DESC, position
        """
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, (ident_val,)).fetchall()
            conn.close()
        return [dict(r) for r in rows]
