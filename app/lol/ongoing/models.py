"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MatchSummary:
    champion_id: int = 0
    champion_name: str = ""
    win: bool = False
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    duration_seconds: int = 0
    game_creation: int = 0  # epoch ms


@dataclass
class PlayerSnapshot:
    puuid: str = ""
    summoner_id: int = 0
    display_name: str = ""
    summoner_level: int = 0
    team: str = "myTeam"  # myTeam / theirTeam
    cell_id: int = 0
    assigned_position: Optional[str] = None
    ranked_tier: Optional[str] = None
    ranked_division: Optional[str] = None
    ranked_lp: int = 0
    recent_matches: List[MatchSummary] = field(default_factory=list)
    # 近场战绩可用性：AVAILABLE / NO_DATA(请求成功但无对局) / ERROR(请求失败/解析失败)
    data_state: str = "ERROR"
    kda_ratio: float = 0.0
    win_rate: float = 0.0
    loaded_at: float = 0.0
    load_error: Optional[str] = None
