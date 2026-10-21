"""
游戏快照 - 统一当前游戏状态

从 2999 API 读取原始数据，整理成统一的 GameSnapshot 结构。
所有下游模块（StateDiff、GameAlarm、UI、SpeechJudge）都基于这个结构。
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlayerInfo:
    """玩家信息"""
    summoner_name: str = ""
    riot_id: str = ""  # full Riot ID "name#tag"
    riot_game_name: str = ""
    riot_tag_line: str = ""
    puuid: str = ""  # PUUID（用于查询 Mastery / 历史战绩）
    summoner_id: int = 0  # summonerId（2999 部分模式返回；InProgress retry 兜底用）
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
    items: list[dict[str, Any]] = field(default_factory=list)
    summoner_spells: list[dict[str, Any]] = field(default_factory=list)
    abilities: dict[str, Any] = field(default_factory=dict)
    runes: list[dict[str, Any]] = field(default_factory=list)
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
    current_player: PlayerInfo | None = None
    players: list[PlayerInfo] = field(default_factory=list)
    ally_team: list[PlayerInfo] = field(default_factory=list)
    enemy_team: list[PlayerInfo] = field(default_factory=list)

    # 队伍信息
    ally_team_name: str = "ORDER"
    enemy_team_name: str = "CHAOS"
    ally_champions: list[str] = field(default_factory=list)
    enemy_champions: list[str] = field(default_factory=list)

    # 中立资源
    objectives: list[ObjectiveState] = field(default_factory=list)

    # 事件（从 /eventdata 获取）
    recent_events: list[dict[str, Any]] = field(default_factory=list)

    # 原始数据（用于调试）
    raw_data: dict[str, Any] = field(default_factory=dict)

    # 元信息
    snapshot_time: float = 0.0
    data_source: str = "live_client"

    def get_player_by_name(self, name: str) -> PlayerInfo | None:
        """按名字查找玩家"""
        for p in self.players:
            if p.summoner_name == name or p.champion_name == name:
                return p
        return None

    def get_player_by_champion(self, champion: str) -> PlayerInfo | None:
        """按英雄查找玩家"""
        for p in self.players:
            if p.champion_name.lower() == champion.lower():
                return p
        return None

    def get_enemy_players(self) -> list[PlayerInfo]:
        """获取敌方玩家"""
        return self.enemy_team

    def get_ally_players(self) -> list[PlayerInfo]:
        """获取友方玩家"""
        return self.ally_team

    def to_dict(self) -> dict[str, Any]:
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

    def _player_to_dict(self, player: PlayerInfo | None) -> dict[str, Any] | None:
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
    def from_api_data(all_data: dict[str, Any],
                      active_player: dict[str, Any] | None = None,
                      player_list: list[dict[str, Any]] | None = None,
                      player_scores: list[dict[str, Any]] | None = None,
                      player_items: list[dict[str, Any]] | None = None,
                      summoner_spells: list[dict[str, Any]] | None = None,
                      game_stats: dict[str, Any] | None = None,
                      event_data: dict[str, Any] | None = None) -> GameSnapshot:
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

            # 先确定当前玩家所在队伍，另一个队伍就是敌方
            my_team = None
            for p_data in all_players:
                if p_data.get("summonerName", "") == active_player_name:
                    my_team = p_data.get("team", "ORDER")
                    break
            if my_team is None:
                my_team = "ORDER"  # 默认

            for p_data in all_players:
                player = SnapshotBuilder._parse_player(p_data, active_player_name)
                snapshot.players.append(player)

                if player.is_current_player:
                    snapshot.current_player = player

                if player.team == my_team:
                    snapshot.ally_team.append(player)
                    snapshot.ally_champions.append(player.champion_name)
                else:
                    snapshot.enemy_team.append(player)
                    snapshot.enemy_champions.append(player.champion_name)

            # 事件（数据边界：events 可能不是 dict，容错）
            events_raw = all_data.get("events", {})
            if isinstance(events_raw, dict):
                events = events_raw.get("Events", [])
            elif isinstance(events_raw, list):
                events = events_raw
            else:
                events = []
            snapshot.recent_events = events[-50:] if isinstance(events, list) and events else []

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
    def _parse_player(p_data: dict[str, Any],
                      active_player_name: str) -> PlayerInfo:
        """解析单个玩家数据"""
        player = PlayerInfo()
        player.summoner_name = p_data.get("summonerName", "")
        # Riot ID fields (2999 now returns these)
        player.riot_id = p_data.get("riotId", "")
        player.riot_game_name = p_data.get("riotIdGameName", "")
        player.riot_tag_line = p_data.get("riotIdTagLine", "")
        # 多试几个 PUUID 字段名
        player.puuid = (p_data.get("puuid") or p_data.get("PUUID") or 
                        p_data.get("summonerId") or p_data.get("userId") or "")
        player.champion_name = p_data.get("championName", "")
        player.champion_id = int(p_data.get("championId", 0))
        player.summoner_id = int(p_data.get("summonerId") or 0)
        player.raw_champion_name = p_data.get("rawChampionName", "")
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
        player.items = p_data.get("items", []) or []

        # 符文（2999 runes 为 dict：keystone/primaryRuneTree/secondaryRuneTree）
        # 提取为结构化 list：[{"type": ..., "id": ..., "displayName": ...}, ...]
        # 空值防御：runes 为空/非 dict 时返回 []；子 dict 缺 id 或 displayName 时跳过该项。
        _raw_runes = p_data.get("runes") or {}
        if isinstance(_raw_runes, dict):
            player.runes = [
                {"type": t, "id": d.get("id"), "displayName": d.get("displayName")}
                for t, d in (
                    ("keystone", _raw_runes.get("keystone") or {}),
                    ("primary", _raw_runes.get("primaryRuneTree") or {}),
                    ("secondary", _raw_runes.get("secondaryRuneTree") or {}),
                )
                if isinstance(d, dict) and d.get("id") is not None and d.get("displayName")
            ]
        else:
            player.runes = _raw_runes or []

        # 召唤师技能
        spells = p_data.get("summonerSpells") or {}
        if not isinstance(spells, dict):
            spells = {}
        player.summoner_spells = [
            spells.get("summonerSpellOne") or {},
            spells.get("summonerSpellTwo") or {},
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
    def _parse_active_player(p_data: dict[str, Any]) -> PlayerInfo:
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

