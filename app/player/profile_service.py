"""PlayerProfileService - 本地历史数据聚合层（只读，纯统计）"""
from typing import Optional, Dict, Any, List

from .history_repository import HistoryRepository


class PlayerProfileService:
    def __init__(self, history_repo: HistoryRepository):
        self.history = history_repo

    @staticmethod
    def _resolve_limit(limit_matches):
        """Normalize limit_matches.
        - None / 0 / negative: explicit semantics
          None → all matches
          0 → empty profile set (no matches used)
          negative → ValueError (explicit, not SQLite LIMIT -1 trick)
        """
        if limit_matches is None:
            return None
        if isinstance(limit_matches, bool):
            raise ValueError("limit_matches must be int or None, got bool")
        if not isinstance(limit_matches, int):
            raise ValueError(f"limit_matches must be int or None, got {type(limit_matches).__name__}")
        if limit_matches < 0:
            raise ValueError(f"limit_matches must be >= 0, got {limit_matches}")
        return limit_matches

    def get_player_profile(self, puuid: Optional[str] = None,
                           riot_id: Optional[str] = None,
                           limit_matches: int = 20,
                           include_incomplete: bool = False) -> Optional[Dict[str, Any]]:
        if not puuid and not riot_id:
            return None

        limit = self._resolve_limit(limit_matches)

        # Fetch all matches (completed + incomplete) for identity, capped high
        all_matches = self.history.get_player_matches(
            puuid=puuid, riot_id=riot_id,
            limit=10000, include_incomplete=True,
        )
        if not all_matches:
            return None

        # Sort by start_time DESC (deterministic: start_time is unique float)
        sorted_all = sorted(all_matches, key=lambda m: (m.get("start_time") or 0, m["local_match_id"]), reverse=True)

        # Determine the Profile analysis set (recent N matches per include_incomplete)
        # If include_incomplete=False, we first filter to completed, then take N.
        # If include_incomplete=True, we take N from all (completed + incomplete).
        if include_incomplete:
            pool_for_limit = sorted_all
        else:
            pool_for_limit = [m for m in sorted_all if m.get("end_time") is not None]

        if limit is None:
            analysis_pool = pool_for_limit
        else:
            analysis_pool = pool_for_limit[:limit]

        # completed/incomplete counts:
        # - sample.completed_games/incomplete_games reflect TOTAL for identity (all matches)
        # - analyzed_games reflects completed within analysis_pool
        total_completed = len([m for m in all_matches if m.get("end_time") is not None])
        total_incomplete = len([m for m in all_matches if m.get("end_time") is None])
        completed_in_pool = [m for m in analysis_pool if m.get("end_time") is not None]
        incomplete_in_pool = [m for m in analysis_pool if m.get("end_time") is None]

        # recent_matches: depends on include_incomplete
        if include_incomplete:
            recent = analysis_pool
        else:
            recent = completed_in_pool  # already ordered DESC

        # Build recent_summaries
        recent_summaries = []
        for m in recent:
            players = self.history.get_match_players(m["local_match_id"])
            me = self._match_me(players, puuid, riot_id)
            recent_summaries.append({
                "local_match_id": m["local_match_id"],
                "start_time": m.get("start_time"),
                "end_time": m.get("end_time"),
                "duration": m.get("duration"),
                "result": m.get("result"),
                "champion_name": (me or {}).get("champion_name"),
                "champion_id": (me or {}).get("champion_id"),
                "team": (me or {}).get("team"),
                "position": (me or {}).get("position"),
                "kills": (me or {}).get("kills"),
                "deaths": (me or {}).get("deaths"),
                "assists": (me or {}).get("assists"),
                "gold": (me or {}).get("gold"),
            })

        # champion_stats / role_stats: only from completed matches IN analysis_pool
        champ_agg: Dict[str, Dict[str, Any]] = {}
        role_agg: Dict[str, Dict[str, Any]] = {}
        for m in completed_in_pool:
            players = self.history.get_match_players(m["local_match_id"])
            me = self._match_me(players, puuid, riot_id)
            if not me:
                continue
            champ = me.get("champion_name") or ""
            pos = me.get("position") or "UNKNOWN"
            result = m.get("result")

            c = champ_agg.setdefault(champ, {
                "champion_name": champ, "games": 0, "wins": 0, "losses": 0,
                "kills": 0, "deaths": 0, "assists": 0,
            })
            c["games"] += 1
            if result == "Win":
                c["wins"] += 1
            elif result == "Lose":
                c["losses"] += 1
            c["kills"] += me.get("kills") or 0
            c["deaths"] += me.get("deaths") or 0
            c["assists"] += me.get("assists") or 0

            r = role_agg.setdefault(pos, {
                "position": pos, "games": 0, "wins": 0, "losses": 0,
            })
            r["games"] += 1
            if result == "Win":
                r["wins"] += 1
            elif result == "Lose":
                r["losses"] += 1

        champion_stats = sorted(champ_agg.values(), key=lambda x: (-x["games"], x["champion_name"]))
        for s in champion_stats:
            g = s["games"]
            s["avg_kills"] = round(s["kills"] / g, 2) if g else 0.0
            s["avg_deaths"] = round(s["deaths"] / g, 2) if g else 0.0
            s["avg_assists"] = round(s["assists"] / g, 2) if g else 0.0

        role_stats = sorted(role_agg.values(), key=lambda x: (-x["games"], x["position"]))

        # data_quality over completed_in_pool
        missing_result = sum(1 for m in completed_in_pool if not m.get("result"))
        missing_champ = missing_pos = missing_kda = zero_gold = 0
        for m in completed_in_pool:
            players = self.history.get_match_players(m["local_match_id"])
            me = self._match_me(players, puuid, riot_id)
            if not me:
                continue
            if not me.get("champion_name"):
                missing_champ += 1
            if not me.get("position"):
                missing_pos += 1
            if me.get("kills") is None or me.get("deaths") is None or me.get("assists") is None:
                missing_kda += 1
            if me.get("gold") == 0:
                zero_gold += 1

        return {
            "identity": {"puuid": puuid, "riot_id": riot_id},
            "sample": {
                "completed_games": total_completed,
                "incomplete_games": total_incomplete,
                "analyzed_games": len(completed_in_pool),
            },
            "recent_matches": recent_summaries,
            "champion_stats": champion_stats,
            "role_stats": role_stats,
            "data_quality": {
                "matches_with_missing_result": missing_result,
                "matches_with_missing_champion": missing_champ,
                "matches_with_missing_position": missing_pos,
                "matches_with_missing_kda": missing_kda,
                "matches_with_zero_gold": zero_gold,
            },
        }

    @staticmethod
    def _match_me(players, puuid, riot_id):
        for p in players:
            if puuid and p.get("puuid") == puuid:
                return p
        for p in players:
            if riot_id and p.get("riot_id") == riot_id:
                return p
        return None
