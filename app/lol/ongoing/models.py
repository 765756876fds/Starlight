"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MatchSummary:
    champion_id: int = 0
    champion_name: str = ""
    win: bool | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
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
    assigned_position: str | None = None
    ranked_tier: str | None = None
    ranked_division: str | None = None
    ranked_lp: int = 0
    # 灵活组排段位
    ranked_flex_tier: str | None = None
    ranked_flex_division: str | None = None
    ranked_flex_lp: int = 0
    recent_matches: list[MatchSummary] = field(default_factory=list)
    # 近场战绩可用性：AVAILABLE / NO_DATA(请求成功但无对局) / ERROR(请求失败/解析失败)
    data_state: str = "ERROR"
    kda_ratio: float = 0.0
    win_rate: float = 0.0
    win_streak: int = 0          # >0 连胜, <0 连败, 0 无数据
    top_champions: list[str] = field(default_factory=list)  # 常用英雄 Top3
    total_matches: int = 0              # 计入统计的匹配+排位场次
    main_position: str | None = None
    current_champion_id: int = 0  # 当前选的英雄 ID
    loaded_at: float = 0.0
    load_error: str | None = None
    # 实时局内数据（来自 liveclientdata 合并，无 puuid 也能稳定携带）
    live_kills: int = 0
    live_deaths: int = 0
    live_assists: int = 0
    live_position: str | None = None

