"""Player data models - identity unification + current game snapshot"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class PlayerIdentity:
    """统一玩家身份

    PUUID 是稳定主键。Riot ID / Summoner ID 是展示/平台相关字段。
    """
    puuid: str = ""
    game_name: str = ""
    tag_line: str = ""
    summoner_id: int = 0
    platform: str = ""

    def key(self) -> str:
        return self.puuid or f"summoner:{self.platform}:{self.summoner_id}"

    def display_name(self) -> str:
        if self.game_name and self.tag_line:
            return f"{self.game_name}#{self.tag_line}"
        return self.game_name or f"summoner_{self.summoner_id}"


@dataclass
class GamePlayer:
    """一局游戏中的玩家（身份 + 本局本局信息）"""
    identity: PlayerIdentity = field(default_factory=PlayerIdentity)
    summoner_name: str = ""  # 2999 返回的名字
    team: str = ""  # ORDER / CHAOS
    champion_name: str = ""
    champion_id: int = 0
    position: str = ""
    is_self: bool = False


@dataclass
class CurrentGamePlayers:
    """当前对局 10 人快照"""
    self_player: Optional[GamePlayer] = None
    allies: List[GamePlayer] = field(default_factory=list)
    enemies: List[GamePlayer] = field(default_factory=list)

    def all(self) -> List[GamePlayer]:
        result = []
        if self.self_player:
            result.append(self.self_player)
        result.extend(self.allies)
        result.extend(self.enemies)
        return result

    def summary(self) -> Dict[str, Any]:
        return {
            "self": self.self_player.summoner_name if self.self_player else None,
            "ally_count": len(self.allies),
            "enemy_count": len(self.enemies),
            "allies": [p.champion_name for p in self.allies],
            "enemies": [p.champion_name for p in self.enemies],
        }
