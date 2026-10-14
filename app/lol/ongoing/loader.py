"""OngoingGameLoader：从 champ-select session 加载 10 玩家信息。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from .cache import SnapshotCache
from .match_model import MatchHistoryResult, MatchRecord, NO_DATA, _VALID_QUEUE_IDS
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
        fetch_count: int = 50,
        account_history_service: Any = None,
    ):
        self._get = lcu_get
        self._on_event = on_event or (lambda c, d: None)
        self._cache = cache or SnapshotCache()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._limiter = RateLimiter(rps)
        self._recent_n = recent_matches_count
        self._fetch_count = fetch_count
        self._acct = account_history_service
        self._errors: List[str] = []
        self._loading = False

    def clear(self) -> None:
        self._cache.clear()
        self._errors.clear()

    def missing_history_count(self) -> int:
        """统计缓存中需要自动重试的失败快照数量。

        只统计 ERROR（请求失败、可能因 SGP 瞬态/断连窗口，值得重试）；
        NO_DATA 是请求成功但过滤后无匹配/排位（业务事实，可缓存复用），
        不重复请求，否则人机局/新手队友会每 60s 触发全量 force 重载，
        导致 UI 周期性清空为"暂无数据"并浪费 SGP 请求。
        """
        return sum(
            1 for s in self._cache.all()
            if getattr(s, "data_state", "ERROR") == "ERROR"
        )

    def get_cached(self, puuid: str) -> Optional[PlayerSnapshot]:
        return self._cache.get(puuid)

    def status(self) -> dict:
        return {
            "loading": self._loading,
            "cached": self._cache.size(),
            "errors": list(self._errors),
        }

    # 缓存语义（借鉴 Akari：成功数据可缓存复用；失败状态不能成为后续 retry 的阻断缓存）
    # AVAILABLE: 请求成功且数据完整 -> 正常刷新可直接复用
    # NO_DATA:   请求成功但确实无有效历史 -> 确定的业务结果，可复用（避免反复请求）
    # ERROR/其他: 请求失败 -> 不能复用；force 重试时必须绕过缓存重新请求
    _REUSABLE_STATES = ("AVAILABLE", "NO_DATA")

    def load_for_champ_select(self, session: dict, force: bool = False) -> List[PlayerSnapshot]:
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
                st = getattr(cached, "data_state", "ERROR")
                if not force and st in self._REUSABLE_STATES:
                    self._on_event("history.cache_hit", {"puuid": p["puuid"], "state": st})
                    snapshots.append(cached)
                else:
                    # ERROR 快照或 force 重试：不能当作有效缓存复用，必须重新请求
                    if st != "AVAILABLE":
                        self._on_event("history.cache_skip", {"puuid": p["puuid"], "state": st})
                    else:
                        self._on_event("history.force_reload", {"puuid": p["puuid"]})
                    pending_puuid.append(p["puuid"])
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
                    "display_name": (cell.get("name") or cell.get("displayName") or 
                                     cell.get("riotIdGameName") or cell.get("riotId") or 
                                     cell.get("summonerName") or ""),
                    "champion_id": cell.get("championId") or 0,
                    # 实时局内数据（liveclientdata 合并，无 puuid 也能稳定携带）
                    "live_kills": cell.get("live_kills", 0) or 0,
                    "live_deaths": cell.get("live_deaths", 0) or 0,
                    "live_assists": cell.get("live_assists", 0) or 0,
                    "live_position": cell.get("live_position") or "",
                })
        return out

    def _load_one(self, base: dict) -> Optional[PlayerSnapshot]:
        puuid = base["puuid"]
        summoner_id = base.get("summoner_id") or 0

        # 1. summoner：by-puuid v2 → v1 → by summonerId
        # 注意：腾讯服游戏内(InProgress) by-puuid 接口实测 404，且 snapshot 路径无 summonerId，
        #       因此 retry 时不能因 summoner 查询失败就中断——必须用旧快照兜底继续，才能走到 history。
        cached_old = self._cache.get(puuid)
        summoner = self._get(f"/lol-summoner/v2/summoners/by-puuid/{puuid}")
        if summoner is None:
            summoner = self._get(f"/lol-summoner/v1/summoners/by-puuid/{puuid}")
        if summoner is None and summoner_id:
            summoner = self._get(f"/lol-summoner/v1/summoners/{summoner_id}")
        if summoner is None and cached_old is not None:
            # 兜底：复用旧快照的 summoner 信息继续（历史战绩仍可查）
            summoner = {
                "summonerId": getattr(cached_old, "summoner_id", 0) or summoner_id or 0,
                "displayName": getattr(cached_old, "display_name", "") or "",
                "summonerLevel": getattr(cached_old, "summoner_level", 0) or 0,
            }
            self._on_event("summoner.fallback_cache", {"puuid": puuid})
        if summoner is None:
            self._errors.append(f"summoner not found: {puuid}")
            return None

        # 2. ranked：按 puuid 查（不能用 current-ranked-stats，那是自己的）
        ranked = self._get(f"/lol-ranked/v1/ranked-stats/{puuid}")
        tier, division, lp = None, None, 0
        flex_tier, flex_division, flex_lp = None, None, 0
        if isinstance(ranked, dict):
            for q in ranked.get("queues", []) or []:
                qt = q.get("queueType")
                if qt == "RANKED_SOLO_5x5":
                    tier = q.get("tier")
                    division = q.get("division")
                    lp = int(q.get("leaguePoints") or 0)
                elif qt == "RANKED_FLEX_SR":
                    flex_tier = q.get("tier")
                    flex_division = q.get("division")
                    flex_lp = int(q.get("leaguePoints") or 0)
        if tier is None and cached_old is not None:
            # ranked 查询失败/不可用：复用旧快照段位信息
            tier = getattr(cached_old, "ranked_tier", None)
            division = getattr(cached_old, "ranked_division", None)
            lp = getattr(cached_old, "ranked_lp", 0) or 0
            flex_tier = getattr(cached_old, "ranked_flex_tier", None)
            flex_division = getattr(cached_old, "ranked_flex_division", None)
            flex_lp = getattr(cached_old, "ranked_flex_lp", 0) or 0

        # 3. 历史战绩：统一走 SGP AccountMatchHistoryService（SUMMARY, count=n）
        result = self._fetch_history(puuid)
        state = result.state
        # 只统计匹配/单双排/灵活排位（白名单，与 intelligence 同口径；自定义/人机/娱乐/未知全排除）
        records = [m for m in result.records if m.queue_id in _VALID_QUEUE_IDS][: self._recent_n]
        excluded = sorted({m.queue_id for m in result.records if m.queue_id not in _VALID_QUEUE_IDS})
        if not records and result.records:
            state = NO_DATA  # 有记录但全是训练/娱乐模式，不参与统计
            if excluded:
                self._on_event("history.queue_filtered", {
                    "puuid": puuid,
                    "excluded_queues": excluded,
                    "kept": len(records),
                })

        wins = sum(1 for m in records if m.win)
        total = len(records)
        k_sum = sum((m.kills or 0) + (m.assists or 0) for m in records)
        d_sum = sum(m.deaths or 0 for m in records) or 1
        top_champs, main_pos = self._summarize(records)
        streak = self._streak(records)
        # 当前英雄 ID
        champ_id = base.get("champion_id", 0)
        
        snap = PlayerSnapshot(
            puuid=puuid,
            summoner_id=int(summoner.get("summonerId") or summoner_id or 0),
            display_name=(summoner.get("displayName") or summoner.get("internalName") or 
                          summoner.get("gameName") or summoner.get("riotIdGameName") or 
                          summoner.get("name") or base["display_name"]),
            current_champion_id=champ_id,
            summoner_level=int(summoner.get("summonerLevel") or 0),
            team=base["team"],
            cell_id=base["cell_id"],
            assigned_position=base["assigned_position"],
            ranked_tier=tier,
            ranked_division=division,
            ranked_lp=lp,
            ranked_flex_tier=flex_tier,
            ranked_flex_division=flex_division,
            ranked_flex_lp=flex_lp,
            recent_matches=[self._to_summary(m) for m in records],
            data_state=state,
            kda_ratio=round(k_sum / d_sum, 2) if state == "AVAILABLE" and records else 0.0,
            win_rate=round(wins / total, 3) if total else 0.0,
            win_streak=streak,
            top_champions=top_champs,
            total_matches=total,
            main_position=main_pos,
            loaded_at=time.monotonic(),
            load_error=None if state == "AVAILABLE" else state,
            live_kills=base.get("live_kills", 0) or 0,
            live_deaths=base.get("live_deaths", 0) or 0,
            live_assists=base.get("live_assists", 0) or 0,
            live_position=base.get("live_position") or None,
        )
        self._cache.put(snap)
        self._on_event("room.player", {
            "puuid": puuid, "name": snap.display_name,
            "matches": total, "wins": wins, "state": state,
        })
        return snap

    def _fetch_history(self, puuid: str) -> MatchHistoryResult:
        if self._acct is None:
            self._on_event("history.error", {"puuid": puuid, "state": "ERROR", "message": "no_account_service"})
            return MatchHistoryResult(state="ERROR", message="no_account_service")
        self._on_event("history.request", {"puuid": puuid, "source": "SGP", "count": self._fetch_count})
        try:
            r = self._acct.recent(puuid, count=self._fetch_count)
            if r.state == "AVAILABLE":
                self._on_event("history.success", {"puuid": puuid, "matches": len(r.records)})
            else:
                self._on_event("history.error", {"puuid": puuid, "state": r.state, "message": r.message})
            return r
        except Exception as e:  # 单人失败隔离
            self._errors.append(f"history failed {puuid}: {e}")
            self._on_event("history.error", {"puuid": puuid, "state": "ERROR", "message": f"exception: {e}"})
            return MatchHistoryResult(state="ERROR", message=f"exception: {e}")

    @staticmethod
    def _to_summary(m: MatchRecord):
        from .models import MatchSummary
        return MatchSummary(
            champion_id=m.champion_id,
            champion_name=m.champion_name,
            win=m.win,
            kills=m.kills,
            deaths=m.deaths,
            assists=m.assists,
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


