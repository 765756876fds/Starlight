"""PlayerQueryService - 玩家历史查询只读服务"""
from typing import Optional, Dict, Any

from .history_repository import HistoryRepository
from .profile_service import PlayerProfileService
from .game_analysis import GameAnalysisService


ALLOWED_QUERY_TYPES = {"recent", "champion_stats", "role_stats", "match_analysis"}
ALLOWED_IDENTITY_KEYS = {"puuid", "riot_id"}


class PlayerQueryService:
    def __init__(self, history_repo: HistoryRepository,
                 profile_service: PlayerProfileService,
                 game_analysis_service: GameAnalysisService):
        self.history = history_repo
        self.profile = profile_service
        self.analysis = game_analysis_service

    def query(self, *, query_type: str,
              identity: Optional[Dict[str, str]] = None,
              limit: Optional[int] = None,
              local_match_id: Optional[str] = None) -> Dict[str, Any]:

        # Validate query_type
        if query_type not in ALLOWED_QUERY_TYPES:
            return {
                "query_type": query_type,
                "identity": identity,
                "result": None,
                "data_quality": {"status": "INVALID_QUERY_TYPE"},
            }

        # Validate limit
        if limit is not None:
            if not isinstance(limit, int) or limit < 0:
                return {
                    "query_type": query_type,
                    "identity": identity,
                    "result": None,
                    "data_quality": {"status": "INVALID_LIMIT"},
                }

        # match_analysis: needs local_match_id
        if query_type == "match_analysis":
            if not local_match_id:
                return {
                    "query_type": query_type,
                    "identity": identity,
                    "result": None,
                    "data_quality": {"status": "MISSING_LOCAL_MATCH_ID"},
                }
            result = self.analysis.analyze_match(local_match_id)
            return {
                "query_type": query_type,
                "identity": identity,
                "result": result,
                "data_quality": {"status": "OK" if result else "MATCH_NOT_FOUND"},
            }

        # Identity validation
        if not identity:
            return {
                "query_type": query_type,
                "identity": None,
                "result": None,
                "data_quality": {"status": "MISSING_IDENTITY"},
            }
        puuid = identity.get("puuid")
        riot_id = identity.get("riot_id")
        if not puuid and not riot_id:
            return {
                "query_type": query_type,
                "identity": identity,
                "result": None,
                "data_quality": {"status": "MISSING_IDENTITY"},
            }

        # recent / champion_stats / role_stats via profile service
        lim = limit or 10
        profile = self.profile.get_player_profile(
            puuid=puuid, riot_id=riot_id,
            limit_matches=lim, include_incomplete=False,
        )
        if not profile:
            return {
                "query_type": query_type,
                "identity": identity,
                "result": None,
                "data_quality": {"status": "NO_MATCHES"},
            }

        if query_type == "recent":
            return {
                "query_type": query_type,
                "identity": identity,
                "result": {
                    "recent_matches": profile["recent_matches"],
                    "sample": profile["sample"],
                },
                "data_quality": profile["data_quality"],
            }
        elif query_type == "champion_stats":
            return {
                "query_type": query_type,
                "identity": identity,
                "result": {"champion_stats": profile["champion_stats"]},
                "data_quality": profile["data_quality"],
            }
        elif query_type == "role_stats":
            return {
                "query_type": query_type,
                "identity": identity,
                "result": {"role_stats": profile["role_stats"]},
                "data_quality": profile["data_quality"],
            }

        return {
            "query_type": query_type,
            "identity": identity,
            "result": None,
            "data_quality": {"status": "UNHANDLED"},
        }
