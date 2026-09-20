"""OngoingGameLoader：从 champ-select session 加载 10 玩家信息。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from .cache import SnapshotCache
from .match_model import MatchHistoryResult, MatchRecord
from .models import PlayerSnapshot


class RateLimiter:
    """简单的令牌桶：每秒最多 n 个请求。"""
    def __init__(self, per_second: float = 20.0):
        self._interval = 1.0 / max(1.0, per_second)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self._interval:
                time.sleep(self._interval - delta)
            self._last = time.monotonic()


class OngoingGameLoader:
    def __init__(
        self,
        lcu_get: Callable[[str], Any],
        on_event: Optional[Callable[[str, dict], None]] = None,
        cache: Optional[SnapshotCache] = None,
        max_workers: int = 4,
        rps: float = 20.0,
        recent_matches_count: int = 20,
        account_history_service: Any = None,
    ):
        self._get = lcu_get
        self._on_event = on_event or (lambda c, d: None)
        self._cache = cache or SnapshotCache()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._limiter = RateLimiter(rps)
        self._recent_n = recent_matches_count
        self._acct = account_history_service
        self._errors: List[str] = []
        self._loading = False

    def clear(self) -> None:
        self._cache.clear()
        self._errors.clear()

    def get_cached(self, puuid: str) -> Optional[PlayerSnapshot]:
        return self._cache.get(puuid)

    def status(self) -> dict:
        return {
            "loading": self._loading,
            "cached": self._cache.size(),
            "errors": list(self._errors),
        }

    def load_for_champ_select(self, session: dict) -> List[PlayerSnapshot]:
        players = self._extract_players(session)
        if not players:
            return []
        seen=set(); uniq=[]
        for p in players:
            if p['puuid'] in seen: continue
            seen.add(p['puuid']); uniq.append(p)
        players = uniq

        self._loading = True
        self._on_event("ongoing.started", {"count": len(players)})

        snapshots: List[PlayerSnapshot] = []
        pending_puuid: List[str] = []
        for p in players:
            cached = self._cache.get(p["puuid"])
            if cached is not None:
                snapshots.append(cached)
            else:
                pending_puuid.append(p["puuid"])

        futures = {
            self._executor.submit(self._load_one, p): p for p in players
            if p["puuid"] in pending_puuid
        }
        for fut in as_completed(futures):
            base = futures[fut]
            try:
                snap = fut.result()
                if snap is not None:
                    snapshots.append(snap)
            except Exception as e:
                self._errors.append(f"{base.get('puuid','?')}: {e}")

        self._loading = False
        self._on_event("ongoing.loaded", {
            "count": len(snapshots),
            "errors": list(self._errors),
        })
        return snapshots

    # ----- internals -----
    def _extract_players(self, session: dict) -> List[dict]:
        """从 myTeam / theirTeam 提取 10 个 (puuid, summonerId, cellId, team, position)。"""
        out = []
        for team_key in ("myTeam", "theirTeam"):
            for cell in (session.get(team_key) or []):
                puuid = cell.get("puuid")
                if not puuid:
                    continue
                out.append({
                    "puuid": puuid,
                    "summoner_id": cell.get("summonerId") or 0,
                    "cell_id": cell.get("cellId") or 0,
                    "team": team_key,
                    "assigned_position": cell.get("assignedPosition"),
                    "display_name": cell.get("name") or cell.get("displayName") or "",
                })
        return out

    def _load_one(self, base: dict) -> Optional[PlayerSnapshot]:
        puuid = base["puuid"]
        summoner_id = base.get("summoner_id") or 0

        # 1. summoner：by-puuid v2 → v1 → by summonerId
        summoner = self._get(f"/lol-summoner/v2/summoners/by-puuid/{puuid}")
        if summoner is None:
            summoner = self._get(f"/lol-summoner/v1/summoners/by-puuid/{puuid}")
        if summoner is None and summoner_id:
            summoner = self._get(f"/lol-summoner/v1/summoners/{summoner_id}")
        if summoner is None:
            self._errors.append(f"summoner not found: {puuid}")
            return None

        # 2. ranked：按 puuid 查（不能用 current-ranked-stats，那是自己的）
        ranked = self._get(f"/lol-ranked/v1/ranked-stats/{puuid}")
        tier, division, lp = None, None, 0
        if isinstance(ranked, dict):
            for q in ranked.get("queues", []) or []:
                if q.get("queueType") == "RANKED_SOLO_5x5":
                    tier = q.get("tier")
                    division = q.get("division")
                    lp = int(q.get("leaguePoints") or 0)
                    break

        # 3. 历史战绩：统一走 SGP AccountMatchHistoryService（SUMMARY, count=n）
        result = self._fetch_history(puuid)
        records = result.records
        state = result.state

        wins = sum(1 for m in records if m.win)
        total = len(records)
        k_sum = sum((m.kills or 0) + (m.assists or 0) for m in records)
        d_sum = sum(m.deaths or 0 for m in records) or 1
        top_champs, main_pos = self._summarize(records)
        streak = self._streak(records)
        snap = PlayerSnapshot(
            puuid=puuid,
            summoner_id=int(summoner.get("summonerId") or summoner_id or 0),
            display_name=summoner.get("displayName") or summoner.get("internalName") or base["display_name"],
            summoner_level=int(summoner.get("summonerLevel") or 0),
            team=base["team"],
            cell_id=base["cell_id"],
            assigned_position=base["assigned_position"],
            ranked_tier=tier,
            ranked_division=division,
            ranked_lp=lp,
            recent_matches=[self._to_summary(m) for m in records],
            data_state=state,
            kda_ratio=round(k_sum / d_sum, 2) if state == "AVAILABLE" and records else 0.0,
            win_rate=round(wins / total, 3) if total else 0.0,
            win_streak=streak,
            top_champions=top_champs,
            main_position=main_pos,
            loaded_at=time.monotonic(),
            load_error=None if state == "AVAILABLE" else state,
        )
        self._cache.put(snap)
        self._on_event("room.player", {
            "puuid": puuid, "name": snap.display_name,
            "matches": total, "wins": wins, "state": state,
        })
        return snap

    def _fetch_history(self, puuid: str) -> MatchHistoryResult:
        if self._acct is None:
            return MatchHistoryResult(state="ERROR", message="no_account_service")
        try:
            return self._acct.recent(puuid, count=self._recent_n)
        except Exception as e:  # 单人失败隔离
            self._errors.append(f"history failed {puuid}: {e}")
            return MatchHistoryResult(state="ERROR", message=f"exception: {e}")

    @staticmethod
    def _to_summary(m: MatchRecord):
        from .models import MatchSummary
        return MatchSummary(
            champion_id=m.champion_id,
            champion_name=m.champion_name,
            win=bool(m.win) if m.win is not None else False,
            kills=m.kills or 0,
            deaths=m.deaths or 0,
            assists=m.assists or 0,
            duration_seconds=m.game_duration,
            game_creation=m.game_creation,
        )

    @staticmethod
    def _summarize(records: List[MatchRecord]):
        """Top3 常用英雄(次数降序, 同次数最近优先) + 主要位置(出现最多)。"""
        if not records:
            return [], None
        champ_count: dict = {}
        champ_last: dict = {}
        pos_count: dict = {}
        for m in records:
            cn = m.champion_name or "?"
            champ_count[cn] = champ_count.get(cn, 0) + 1
            champ_last[cn] = max(champ_last.get(cn, 0), m.game_creation)
            pos = (m.team_position or "").upper()
            if pos:
                pos_count[pos] = pos_count.get(pos, 0) + 1
        ranked = sorted(champ_count, key=lambda c: (-champ_count[c], -champ_last[c]))
        top3 = ranked[:3]
        main_pos = max(pos_count, key=pos_count.get) if pos_count else None
        return top3, main_pos

    @staticmethod
    def _streak(records: List[MatchRecord]) -> int:
        """最新→最旧的连续胜/负。返回 >0 连胜, <0 连败。"""
        if not records:
            return 0
        first_win = records[0].win
        if first_win is None:
            return 0
        n = 0
        for m in records:  # records 已按 game_creation 降序
            if m.win is None or m.win != first_win:
                break
            n += 1
        return n if first_win else -n
