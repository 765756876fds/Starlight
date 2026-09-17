"""GameAnalysisService - 单局比赛事实分析层（只读，确定性）"""
from typing import Optional, Dict, Any, List

from .history_repository import HistoryRepository
from .event_log_reader import EventLogReader


class GameAnalysisService:
    def __init__(self, history_repo: HistoryRepository,
                 event_log_reader: Optional[EventLogReader] = None):
        self.history = history_repo
        self.reader = event_log_reader or EventLogReader()

    def analyze_match(self, local_match_id: str) -> Optional[Dict[str, Any]]:
        match = self.history.get_match(local_match_id)
        if not match:
            return None

        rec = match["match"]
        players = match["players"]

        # self: MatchPlayer.is_self=1 is the ONLY identity source.
        # No players[0] / position / name fallback. If is_self is absent,
        # self_player is None and data_quality.self_player = MISSING.
        self_player = next((p for p in players if p.get("is_self")), None)

        # events
        events = self.reader.read_for_match(local_match_id)

        # Categorize events
        counts = {
            "total": len(events),
            "champion_kills": 0,
            "first_blood": 0,
            "multikills": 0,
            "tower_destroyed": 0,
            "dragon_killed": 0,
            "herald_killed": 0,
            "baron_killed": 0,
            "inhibitor_destroyed": 0,
            "ace": 0,
        }
        first_blood_info = None
        multikill_counts = {"double": 0, "triple": 0, "quadra": 0, "penta": 0}
        timeline: List[Dict[str, Any]] = []

        for ev in events:
            et = ev["event_type"]
            payload = ev["payload"]
            game_time = payload.get("game_time")

            if et == "game.champion_kill":
                counts["champion_kills"] += 1
                timeline.append({
                    "event_type": "champion_kill",
                    "game_time": game_time,
                    "killer_name": payload.get("killer_name"),
                    "victim_name": payload.get("victim_name"),
                    "killer_team": payload.get("killer_team"),
                })
            elif et == "game.first_blood":
                counts["first_blood"] += 1
                first_blood_info = {
                    "occurred": True,
                    "game_time": game_time,
                    "killer": payload.get("killer_name"),
                    "victim": payload.get("victim_name"),
                }
                timeline.append({
                    "event_type": "first_blood",
                    "game_time": game_time,
                    "killer": payload.get("killer_name"),
                    "victim": payload.get("victim_name"),
                })
            elif et == "game.multi_kill":
                counts["multikills"] += 1
                streak = payload.get("kill_streak")
                if streak == 2:
                    multikill_counts["double"] += 1
                elif streak == 3:
                    multikill_counts["triple"] += 1
                elif streak == 4:
                    multikill_counts["quadra"] += 1
                elif streak and streak >= 5:
                    multikill_counts["penta"] += 1
                timeline.append({
                    "event_type": "multikill",
                    "game_time": game_time,
                    "kill_streak": streak,
                    "killer_name": payload.get("killer_name"),
                })
            elif et == "game.tower_destroyed":
                counts["tower_destroyed"] += 1
                timeline.append({
                    "event_type": "tower_destroyed",
                    "game_time": game_time,
                    "killer_name": payload.get("killer_name"),
                    "tower_name": payload.get("tower_name"),
                })
            elif et == "game.dragon_killed":
                counts["dragon_killed"] += 1
                timeline.append({
                    "event_type": "dragon_killed",
                    "game_time": game_time,
                    "killer_name": payload.get("killer_name"),
                    "dragon_type": payload.get("dragon_type"),
                })
            elif et == "game.herald_killed":
                counts["herald_killed"] += 1
                timeline.append({
                    "event_type": "herald_killed",
                    "game_time": game_time,
                    "killer_name": payload.get("killer_name"),
                })
            elif et == "game.baron_killed":
                counts["baron_killed"] += 1
                timeline.append({
                    "event_type": "baron_killed",
                    "game_time": game_time,
                    "killer_name": payload.get("killer_name"),
                })
            elif et == "game.inhibitor_destroyed":
                counts["inhibitor_destroyed"] += 1
                timeline.append({
                    "event_type": "inhibitor_destroyed",
                    "game_time": game_time,
                    "killer_name": payload.get("killer_name"),
                })
            elif et == "game.ace":
                counts["ace"] += 1
                timeline.append({
                    "event_type": "ace",
                    "game_time": game_time,
                    "killer_team": payload.get("killer_team"),
                })

        # Sort timeline: game_time ASC, then stable by event_type + insertion order
        timeline.sort(key=lambda x: (x.get("game_time") or 0, x["event_type"]))

        # Self summary
        self_summary = {
            "riot_id": (self_player or {}).get("riot_id"),
            "puuid": (self_player or {}).get("puuid"),
            "champion_name": (self_player or {}).get("champion_name"),
            "champion_id": (self_player or {}).get("champion_id"),
            "team": (self_player or {}).get("team"),
            "position": (self_player or {}).get("position"),
            "kills": (self_player or {}).get("kills"),
            "deaths": (self_player or {}).get("deaths"),
            "assists": (self_player or {}).get("assists"),
            "gold": (self_player or {}).get("gold"),
        }

        # data quality
        dq = {
            "result": "UNKNOWN" if not rec.get("result") else "AVAILABLE",
            "duration": "AVAILABLE" if rec.get("duration") is not None else "MISSING",
            "self_player": "AVAILABLE" if self_player else "MISSING",
            "events_logged": "AVAILABLE" if events else "MISSING",
            "gold_zero_means_unknown": (self_player or {}).get("gold") == 0,
        }

        return {
            "local_match_id": local_match_id,
            "match": {
                "start_time": rec.get("start_time"),
                "end_time": rec.get("end_time"),
                "duration": rec.get("duration"),
                "result": rec.get("result") or "Unknown",
                "game_mode": rec.get("game_mode"),
                "map_name": rec.get("map_name"),
            },
            "self": self_summary,
            "events": counts,
            "first_blood": first_blood_info or {"occurred": False, "note": "NOT_FOUND_IN_EVENTLOG"},
            "multikills": multikill_counts,
            "timeline": timeline,
            "data_quality": dq,
        }
