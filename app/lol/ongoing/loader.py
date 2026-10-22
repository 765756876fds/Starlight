"""OngoingGameLoader：从 champ-select session 加载 10 玩家信息。"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .cache import SnapshotCache
from .match_model import _VALID_QUEUE_IDS, NO_DATA, MatchHistoryResult, MatchRecord
from .models import PlayerSnapshot
from .summoner_privacy import SummonerPrivacyService
from .debug_tracer import _DEBUG_ONGOING, ongoing_logger


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
        on_event: Callable[[str, dict], None] | None = None,
        cache: SnapshotCache | None = None,
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
        self._errors: list[str] = []
        self._loading = False
        self.on_name_resolved = None  # 名字先出回调：查到名字立即通知（service 绑定）

    def clear(self) -> None:
        self._cache.clear()
        self._errors.clear()

    def missing_history_count(self, session_puuids=None) -> int:
        """统计需要自动重试的缺失快照数量。

        - ERROR 快照（请求失败、可能因 SGP 瞬态/断连窗口，值得重试）
        - 当前 session 有 puuid 但 cache 里根本没有其快照（如选人阶段只 resolve 出
          己方 5 人 → 进游戏后 gameflow 10 人全 puuid：敌方 5 人从未加载，
          必须计入缺失，否则 60s 自动重试永不触发）

        NO_DATA 是请求成功但过滤后无匹配/排位（业务事实，可缓存复用），
        不重复请求，否则人机局/新手队友会每 60s 触发全量 force 重载，
        导致 UI 周期性清空为"暂无数据"并浪费 SGP 请求。
        """
        cached = self._cache.all()
        error_n = sum(
            1 for s in cached
            if getattr(s, "data_state", "ERROR") == "ERROR"
        )
        missing_puuid_n = 0
        if session_puuids:
            cached_puuids = {s.puuid for s in cached}
            missing_puuid_n = sum(
                1 for p in session_puuids if p and p not in cached_puuids
            )
        return error_n + missing_puuid_n

    def missing_names_count(self) -> int:
        """统计名字缺失（summoner 查询曾失败）的快照，retry 时重查补名。

        与 missing_history_count 互补：
        - AVAILABLE 空名：history 成功但 summoner 404（腾讯服禁用 by-puuid）
        - NO_DATA 空名：战绩请求成功但同样 summoner 失败（选人 HIDDEN cell
          summonerId=0 导致 by-summonerId 无法查询）——名字/等级缺失同样要让
          retry 触发，重载时用的是 InProgress 的 gameflow session（summonerId
          真实）→ by-summonerId 拿到 gameName#tagLine / summonerLevel 回填，
          修复 UI "? Lv.?"。
        ERROR 不在此统计：由 missing_history_count 的 error_n 覆盖。
        """
        return sum(
            1 for s in self._cache.all()
            if getattr(s, "data_state", "ERROR") != "ERROR"
            and not getattr(s, "display_name", "")
        )

    def get_cached(self, puuid: str) -> PlayerSnapshot | None:
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

    def load_for_champ_select(self, session: dict, force: bool = False) -> list[PlayerSnapshot]:
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

        snapshots: list[PlayerSnapshot] = []
        pending_puuid: list[str] = []
        for p in players:
            cached = self._cache.get(p["puuid"])
            if cached is not None:
                st = getattr(cached, "data_state", "ERROR")
                if not force and st in self._REUSABLE_STATES:
                    if not cached.display_name:
                        # 名字缺失（summoner 曾失败）：AVAILABLE/NO_DATA 都不当作可复用，
                        # 重新请求重查 summoner —— 本修覆盖 NO_DATA 空名：进游戏后
                        # gameflow cell 有真实 summonerId，by-summonerId 可补回名字/等级，
                        # 修复 UI "? Lv.?"。请求数受加载时机（session 创建/升级/60s retry）
                        # 约束，不会每 tick 重发。
                        self._on_event("history.name_missing", {"puuid": p["puuid"]})
                        pending_puuid.append(p["puuid"])
                    else:
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

        # 新增：SGP 批量查询玩家名字（腾讯服 LCU 拿不到名字时的稳定兜底通道）
        puuids_to_check = [p.get("puuid") for p in players if p.get("puuid") in pending_puuid]
        self._sgp_info_cache = {}
        if puuids_to_check:
            try:
                if _DEBUG_ONGOING:
                    ongoing_logger.debug(f"SGP Name Req start: puuids={len(puuids_to_check)}")
                if not getattr(self, "_privacy_service", None):
                    self._privacy_service = SummonerPrivacyService(self._get)
                self._sgp_info_cache = self._privacy_service.batch_summoner_info(puuids_to_check)
                if _DEBUG_ONGOING:
                    ongoing_logger.debug(f"SGP Name Req done: got {len(self._sgp_info_cache)} results")
                # 批量拿到名字后立刻全员回调（不等 _load_one 慢慢查战绩）：
                # 10 人名字同帧落地 → UI 一次性渲染，消除"名字逐个蹦出"的闪烁
                for _pu, _info in self._sgp_info_cache.items():
                    _gn = (_info or {}).get("name")
                    if _gn and getattr(self, "on_name_resolved", None):
                        _tl = (_info or {}).get("tagLine") or ""
                        try:
                            self.on_name_resolved(_pu, f"{_gn}#{_tl}" if _tl else _gn)
                        except Exception:
                            pass
            except Exception as _e:
                self._sgp_info_cache = {}
                self._errors.append("sgp batch failed: %s" % _e)
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
    def _extract_players(self, session: dict) -> list[dict]:
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

    def _load_one(self, base: dict) -> PlayerSnapshot | None:
        puuid = base["puuid"]
        summoner_id = base.get("summoner_id") or 0
        summoner_failed = False

        # 1. summoner：by-puuid v2 → v1 → by summonerId
        # 注意：腾讯服 LCU 服务端全局禁用 by-puuid 接口（自己+队友都 404，
        #       实测 "Invalid URI format"）；InProgress/EndOfGame 阶段 gameflow
        #       cell 的 summonerId 是真实的（选人 HIDDEN 阶段才为 0），
        #       因此 by-summonerId 是队友名字（gameName#tagLine）的主通道。
        #       查询失败不能中断——必须继续走 ranked/history（SGP 不依赖
        #       LCU summoner 接口），名字缺失由 retry（missing_names_count）补。
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
            summoner_failed = True
            self._errors.append(f"summoner not found: {puuid}")
            summoner = {}

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
        # 名字 = gameName#tagLine（腾讯服 Riot ID 格式，如"辅助之半步青铜#40396"；
        # current-summoner 与 by-summonerId 响应均含这两个字段，displayName 恒空）
        gn = (summoner.get("gameName") or summoner.get("displayName")
              or summoner.get("internalName") or summoner.get("riotIdGameName")
              or summoner.get("name") or "")
        tl = summoner.get("tagLine") or ""
        # 新增：SGP 名字兜底（解决腾讯服 LCU 没名字导致 "?" 的问题）
        if not gn and getattr(self, "_sgp_info_cache", None):
            sgp_info = self._sgp_info_cache.get(puuid)
            if sgp_info:
                gn = sgp_info.get("name") or ""
                tl = sgp_info.get("tagLine") or tl
        # 名字先出：查到名字立即回调（service 回填 session，UI 秒显示），不等 SGP 战绩
        if gn:
            _disp = f"{gn}#{tl}" if tl else gn
            if _DEBUG_ONGOING:
                ongoing_logger.debug(f"Name Resolved: {puuid[:6]}... -> {_disp}")
            if getattr(self, "on_name_resolved", None):
                try:
                    self.on_name_resolved(puuid, _disp)
                except Exception:
                    pass
        
        snap = PlayerSnapshot(
            puuid=puuid,
            summoner_id=int(summoner.get("summonerId") or summoner_id or 0),
            display_name=(f"{gn}#{tl}" if (gn and tl)
                          else (gn or base["display_name"])),
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
            load_error=("summoner_not_found" if summoner_failed
                        else (None if state == "AVAILABLE" else state)),
            live_kills=base.get("live_kills", 0) or 0,
            live_deaths=base.get("live_deaths", 0) or 0,
            live_assists=base.get("live_assists", 0) or 0,
            live_position=base.get("live_position") or None,
        )
        if _DEBUG_ONGOING:
            ongoing_logger.debug(f"LoadOne done: puuid={puuid[:6]}..., name={gn}, champ={champ_id}, state={state}")
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
    def _summarize(records: list[MatchRecord]):
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
    def _streak(records: list[MatchRecord]) -> int:
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


