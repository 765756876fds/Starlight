"""当前对局 Session 模型：身份 / 实时 / 历史 三区分离。

数据链（唯一主源）：
  Gameflow (teamOne/teamTwo)  ->  CurrentGameSession.players  [identity 唯一来源]
  LiveClientData (2999)       ->  CurrentGamePlayer.realtime   [实时字段唯一来源]
  OngoingGameLoader -> AccountMatchHistoryService -> SGP/LCU
                              ->  CurrentGamePlayer.history    [历史唯一来源]

生命周期：
  game_id 是 session 锚点；新 gameId = 新 session，旧 session 立即失效。
  不允许因 PUUID 相同而跨局复用上一局 PlayerSnapshot（TTL 不承担 session 隔离，
  跨局隔离由 service 在新 session 时强制重新加载实现）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import PlayerSnapshot


@dataclass
class CurrentGamePlayer:
    """当前对局玩家视图。

    - identity：来自 Gameflow（权威身份；PlayerSnapshot 内的同名身份字段仅为兼容保留）
    - realtime：来自 LiveClientData（2999 不可用时 realtime_available=False，字段保持默认值）
    - history：来自 loader -> AccountMatchHistoryService -> SGP/LCU（PlayerSnapshot 在此上下文中仅代表历史）
    """
    # ----- identity (Gameflow) -----
    puuid: str = ""
    summoner_id: int = 0
    display_name: str = ""
    cell_id: int = 0
    team: str = "myTeam"  # myTeam / theirTeam
    assigned_position: str | None = None
    champion_id: int = 0
    is_self: bool = False

    # ----- realtime (LiveClientData 2999) -----
    live_kills: int = 0
    live_deaths: int = 0
    live_assists: int = 0
    live_position: str | None = None
    live_level: int = 0
    realtime_available: bool = False

    # ----- history (loader -> SGP/LCU) -----
    history: PlayerSnapshot | None = None


@dataclass
class CurrentGameSession:
    """当前对局 session。game_id 为生命周期锚点（gameflow 无 gameId 时才允许 fallback key）。"""
    game_id: int = 0
    phase: str = ""
    my_team: list[CurrentGamePlayer] = field(default_factory=list)
    their_team: list[CurrentGamePlayer] = field(default_factory=list)
    started_at: float = 0.0
    updated_at: float = 0.0

    @property
    def players(self) -> list[CurrentGamePlayer]:
        return self.my_team + self.their_team

    @property
    def has_players(self) -> bool:
        return bool(self.my_team or self.their_team)
