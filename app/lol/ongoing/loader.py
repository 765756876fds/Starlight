"""OngoingGameLoader：从 champ-select session 加载 10 玩家信息。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from .cache import SnapshotCache
from .models import MatchSummary, PlayerSnapshot


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
        recent_matches_count: int = 10,
    ):
        self._get = lcu_get
        self._on_event = on_event or (lambda c, d: None)
        self._cache = cache or SnapshotCache()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._limiter = RateLimiter(rps)
        self._recent_n = recent_matches_count
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

        # 3. match history v1 → fallback v3
        matches = self._fetch_matches(puuid)
        load_error = None if matches else "no_matches"

        win = sum(1 for m in matches if m.win)
        total = len(matches)
        k_sum = sum(m.kills + m.assists for m in matches)
        d_sum = sum(m.deaths for m in matches) or 1
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
            recent_matches=matches,
            kda_ratio=round(k_sum / d_sum, 2),
            win_rate=round(win / total, 3) if total else 0.0,
            loaded_at=time.monotonic(),
            load_error=load_error,
        )
        self._cache.put(snap)
        return snap

    def _fetch_matches(self, puuid: str) -> List[MatchSummary]:
        n = self._recent_n
        # v1
        self._limiter.wait()
        data = self._get(
            f"/lol-match-history/v1/products/lol/{puuid}/matches?begIndex=0&endIndex={n}")
        if data is None:
            # v3 fallback
            self._limiter.wait()
            data = self._get(f"/lol-match-history/v3/matchlist/{puuid}")
        if not isinstance(data, dict):
            self._on_event("ongoing.match_history_unavailable", {"puuid": puuid})
            return []

        games = data.get("games", {}).get("games") or data.get("matches") or []
        out: List[MatchSummary] = []
        for g in games[:n]:
            stats = g.get("json") or g.get("stats") or {}
            out.append(MatchSummary(
                champion_id=int(g.get("championId") or 0),
                champion_name="",
                win=bool(stats.get("win", False)),
                kills=int(stats.get("kills") or 0),
                deaths=int(stats.get("deaths") or 0),
                assists=int(stats.get("assists") or 0),
                duration_seconds=int(g.get("timePlayed") or g.get("gameDuration") or 0),
                game_creation=int(g.get("gameCreation") or 0),
            ))
        return out
