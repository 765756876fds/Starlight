"""
游戏快照 - 统一当前游戏状态

从 2999 API 读取原始数据，整理成统一的 GameSnapshot 结构。
所有下游模块（StateDiff、GameAlarm、UI、SpeechJudge）都基于这个结构。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PlayerInfo:
    """玩家信息"""
    summoner_name: str = ""
    champion_name: str = ""
    champion_id: int = 0
    team: str = ""  # "ORDER" / "CHAOS" / "UNKNOWN"
    is_current_player: bool = False
    level: int = 0
    gold: float = 0.0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    cs: int = 0  # 补刀数
    position: str = ""  # top / jungle / mid / bot / utility
    items: List[Dict[str, Any]] = field(default_factory=list)
    summoner_spells: List[Dict[str, Any]] = field(default_factory=list)
    abilities: Dict[str, Any] = field(default_factory=dict)
    runes: List[Dict[str, Any]] = field(default_factory=list)
    respawn_timer: float = 0.0
    health: float = 0.0
    max_health: float = 0.0
    mana: float = 0.0
    max_mana: float = 0.0


@dataclass
class ObjectiveState:
    """中立资源状态"""
    objective_type: str = ""  # dragon / baron / herald / tower
    dragon_type: str = ""     # 对于小龙
    team: str = ""            # 哪一方的（防御塔）或击杀方
    is_alive: bool = True
    respawn_time: float = 0.0  # 剩余刷新时间（秒）
    next_spawn_time: float = 0.0  # 下次刷新的游戏时间
    killed_at: float = 0.0     # 被杀时的游戏时间
    source: str = ""           # 数据来源：api / calculated / fallback


@dataclass
class GameSnapshot:
    """
    游戏快照 - 统一当前状态

    由 LiveClient 数据整理而成。
    """
    # 游戏基本信息
    game_time: float = 0.0
    game_mode: str = ""
    map_name: str = ""
    map_number: int = 0
    is_game_running: bool = False
    is_paused: bool = False

    # 玩家
    current_player: Optional[PlayerInfo] = None
    players: List[PlayerInfo] = field(default_factory=list)
    ally_team: List[PlayerInfo] = field(default_factory=list)
    enemy_team: List[PlayerInfo] = field(default_factory=list)

    # 队伍信息
    ally_team_name: str = "ORDER"
    enemy_team_name: str = "CHAOS"
    ally_champions: List[str] = field(default_factory=list)
    enemy_champions: List[str] = field(default_factory=list)

    # 中立资源
    objectives: List[ObjectiveState] = field(default_factory=list)

    # 事件（从 /eventdata 获取）
    recent_events: List[Dict[str, Any]] = field(default_factory=list)

    # 原始数据（用于调试）
    raw_data: Dict[str, Any] = field(default_factory=dict)

    # 元信息
    snapshot_time: float = 0.0
    data_source: str = "live_client"

    def get_player_by_name(self, name: str) -> Optional[PlayerInfo]:
        """按名字查找玩家"""
        for p in self.players:
            if p.summoner_name == name or p.champion_name == name:
                return p
        return None

    def get_player_by_champion(self, champion: str) -> Optional[PlayerInfo]:
        """按英雄查找玩家"""
        for p in self.players:
            if p.champion_name.lower() == champion.lower():
                return p
        return None

    def get_enemy_players(self) -> List[PlayerInfo]:
        """获取敌方玩家"""
        return self.enemy_team

    def get_ally_players(self) -> List[PlayerInfo]:
        """获取友方玩家"""
        return self.ally_team

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于 UI 显示和日志）"""
        return {
            "game_time": self.game_time,
            "game_mode": self.game_mode,
            "is_game_running": self.is_game_running,
            "current_player": self._player_to_dict(self.current_player),
            "players": [self._player_to_dict(p) for p in self.players],
            "objectives": [vars(o) for o in self.objectives],
            "snapshot_time": self.snapshot_time,
        }

    def _player_to_dict(self, player: Optional[PlayerInfo]) -> Optional[Dict[str, Any]]:
        if player is None:
            return None
        return {
            "summoner_name": player.summoner_name,
            "champion_name": player.champion_name,
            "team": player.team,
            "level": player.level,
            "kills": player.kills,
            "deaths": player.deaths,
            "assists": player.assists,
            "cs": player.cs,
            "gold": player.gold,
        }


class SnapshotBuilder:
    """
    从 2999 API 原始数据构建 GameSnapshot
    """

    @staticmethod
    def from_api_data(all_data: Dict[str, Any],
                      active_player: Optional[Dict[str, Any]] = None,
                      player_list: Optional[List[Dict[str, Any]]] = None,
                      player_scores: Optional[List[Dict[str, Any]]] = None,
                      player_items: Optional[List[Dict[str, Any]]] = None,
                      summoner_spells: Optional[List[Dict[str, Any]]] = None,
                      game_stats: Optional[Dict[str, Any]] = None,
                      event_data: Optional[Dict[str, Any]] = None) -> GameSnapshot:
        """
        从 API 数据构建快照

        优先使用 all_data，其他参数用于补充或覆盖。
        """
        snapshot = GameSnapshot()
        snapshot.snapshot_time = __import__("time").time()

        # 如果有 all_data，从中提取
        if all_data:
            snapshot.raw_data = all_data

            # 游戏基本信息
            game_data = all_data.get("gameData", {})
            snapshot.game_time = float(game_data.get("gameTime", 0))
            snapshot.game_mode = game_data.get("gameMode", "")
            snapshot.map_name = game_data.get("mapName", "")
            snapshot.map_number = int(game_data.get("mapNumber", 0))
            snapshot.is_game_running = game_data.get("gameMode", "") != ""

            # 玩家列表
            all_players = all_data.get("allPlayers", [])
            active_player_name = all_data.get("activePlayer", {}).get("summonerName", "")

            for p_data in all_players:
                player = SnapshotBuilder._parse_player(p_data, active_player_name)
                snapshot.players.append(player)

                if player.is_current_player:
                    snapshot.current_player = player

                if player.team == "ORDER":
                    snapshot.ally_team.append(player)
                    snapshot.ally_champions.append(player.champion_name)
                else:
                    snapshot.enemy_team.append(player)
                    snapshot.enemy_champions.append(player.champion_name)

            # 事件
            events = all_data.get("events", {}).get("Events", [])
            snapshot.recent_events = events[-50:] if events else []

        # 用单独的 API 数据覆盖/补充
        if game_stats:
            snapshot.game_time = float(game_stats.get("gameTime", snapshot.game_time))

        if active_player and snapshot.current_player is None:
            snapshot.current_player = SnapshotBuilder._parse_active_player(active_player)

        if player_list and not snapshot.players:
            for p_data in player_list:
                player = SnapshotBuilder._parse_player(p_data, "")
                snapshot.players.append(player)

        return snapshot

    @staticmethod
    def _parse_player(p_data: Dict[str, Any],
                      active_player_name: str) -> PlayerInfo:
        """解析单个玩家数据"""
        player = PlayerInfo()
        player.summoner_name = p_data.get("summonerName", "")
        player.champion_name = p_data.get("championName", "")
        player.champion_id = int(p_data.get("championId", 0))
        player.team = p_data.get("team", "UNKNOWN")
        player.is_current_player = player.summoner_name == active_player_name
        player.level = int(p_data.get("level", 0))
        player.gold = float(p_data.get("currentGold", 0))

        # 位置
        player.position = p_data.get("position", "").lower()

        # 分数
        scores = p_data.get("scores", {})
        player.kills = int(scores.get("kills", 0))
        player.deaths = int(scores.get("deaths", 0))
        player.assists = int(scores.get("assists", 0))
        player.cs = int(scores.get("creepScore", 0))

        # 物品
        player.items = p_data.get("items", [])

        # 召唤师技能
        spells = p_data.get("summonerSpells", {})
        player.summoner_spells = [
            spells.get("summonerSpellOne", {}),
            spells.get("summonerSpellTwo", {}),
        ]

        # 生命值
        player.health = float(p_data.get("health", 0))
        player.max_health = float(p_data.get("maxHealth", 0))
        player.mana = float(p_data.get("resourceValue", 0))
        player.max_mana = float(p_data.get("resourceMax", 0))

        # 复活计时
        player.respawn_timer = float(p_data.get("respawnTimer", 0))

        return player

    @staticmethod
    def _parse_active_player(p_data: Dict[str, Any]) -> PlayerInfo:
        """解析当前玩家数据"""
        player = PlayerInfo()
        player.summoner_name = p_data.get("summonerName", "")
        player.champion_name = p_data.get("championName", "")
        player.is_current_player = True
        player.level = int(p_data.get("level", 0))
        player.gold = float(p_data.get("currentGold", 0))

        abilities = p_data.get("abilities", {})
        player.abilities = abilities

        return player
