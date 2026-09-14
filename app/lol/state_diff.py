"""
状态差异检测 - 比较新旧 GameSnapshot，产生游戏事件

这是 2999 数据 -> 事件 的核心转换层。
不直接 TTS，而是产生 Event 交给 EventBus。
"""
import time
from typing import Any, Dict, List, Optional, Tuple

from .snapshot import GameSnapshot, PlayerInfo
from .events import GameEvent, GameEventType


class StateDiff:
    """
    比较两个 GameSnapshot，检测状态变化并产生事件
    """

    def __init__(self):
        self._last_snapshot: Optional[GameSnapshot] = None
        self._last_kill_time: float = 0.0
        self._kill_streak: int = 0
        self._last_kill_team: str = ""
        self._known_events: set = set()  # 去重

    def diff(self, new_snapshot: GameSnapshot) -> List[GameEvent]:
        """
        比较新旧快照，产生事件列表

        Args:
            new_snapshot: 新的游戏快照

        Returns:
            产生的事件列表
        """
        events = []

        if self._last_snapshot is None:
            self._last_snapshot = new_snapshot
            return events

        old = self._last_snapshot
        new = new_snapshot

        # 游戏时间更新
        if new.game_time != old.game_time:
            events.append(GameEvent(
                event_type=GameEventType.GAME_TIME_UPDATE,
                game_time=new.game_time,
                data={"game_time": new.game_time},
                timestamp=time.time(),
            ))

        # 游戏开始/结束
        if not old.is_game_running and new.is_game_running:
            events.append(GameEvent(
                event_type=GameEventType.GAME_STARTED,
                game_time=new.game_time,
                data={"game_mode": new.game_mode, "map": new.map_name},
                timestamp=time.time(),
            ))
        elif old.is_game_running and not new.is_game_running:
            events.append(GameEvent(
                event_type=GameEventType.GAME_ENDED,
                game_time=new.game_time,
                data={},
                timestamp=time.time(),
            ))

        # 玩家升级
        events.extend(self._detect_level_ups(old, new))

        # 击杀事件（从 eventdata 检测）
        events.extend(self._detect_kills(old, new))

        # 物品变化
        events.extend(self._detect_item_changes(old, new))

        # 召唤师技能使用
        events.extend(self._detect_summoner_spell_usage(old, new))

        # 防御塔/中立资源（从 eventdata）
        events.extend(self._detect_objectives(old, new))

        # 去重
        events = self._deduplicate(events)

        self._last_snapshot = new_snapshot
        return events

    def _detect_level_ups(self, old: GameSnapshot, new: GameSnapshot) -> List[GameEvent]:
        """检测玩家升级"""
        events = []
        old_players = {p.summoner_name: p for p in old.players}

        for new_p in new.players:
            old_p = old_players.get(new_p.summoner_name)
            if old_p and new_p.level > old_p.level:
                events.append(GameEvent(
                    event_type=GameEventType.PLAYER_LEVEL_UP,
                    game_time=new.game_time,
                    data={
                        "player_name": new_p.summoner_name,
                        "champion_name": new_p.champion_name,
                        "team": new_p.team,
                        "old_level": old_p.level,
                        "new_level": new_p.level,
                        "is_current_player": new_p.is_current_player,
                    },
                    timestamp=time.time(),
                ))
        return events

    def _detect_kills(self, old: GameSnapshot, new: GameSnapshot) -> List[GameEvent]:
        """
        检测击杀事件

        优先从 /eventdata 的 Events 列表中检测。
        """
        events = []

        # 从 eventdata 检测
        old_events = {self._event_key(e) for e in old.recent_events}
        new_events = new.recent_events

        for evt in new_events:
            key = self._event_key(evt)
            if key in old_events:
                continue

            event_name = evt.get("EventName", "")

            if event_name == "ChampionKill":
                killer_name = evt.get("KillerName", "")
                victim_name = evt.get("VictimName", "")
                assistants = evt.get("Assisters", [])

                # 查找队伍
                killer_team = self._find_team(new, killer_name)
                victim_team = self._find_team(new, victim_name)

                events.append(GameEvent(
                    event_type=GameEventType.CHAMPION_KILL,
                    game_time=new.game_time,
                    data={
                        "killer_name": killer_name,
                        "killer_team": killer_team,
                        "victim_name": victim_name,
                        "victim_team": victim_team,
                        "assistants": assistants,
                        "is_current_player_kill": killer_name == (new.current_player.summoner_name if new.current_player else ""),
                        "is_current_player_death": victim_name == (new.current_player.summoner_name if new.current_player else ""),
                    },
                    timestamp=time.time(),
                ))

                # 多杀检测
                if killer_team == self._last_kill_team:
                    self._kill_streak += 1
                else:
                    self._kill_streak = 1
                    self._last_kill_team = killer_team

                if self._kill_streak >= 2:
                    events.append(GameEvent(
                        event_type=GameEventType.MULTI_KILL,
                        game_time=new.game_time,
                        data={
                            "team": killer_team,
                            "count": self._kill_streak,
                        },
                        timestamp=time.time(),
                    ))

            elif event_name == "FirstBlood":
                events.append(GameEvent(
                    event_type=GameEventType.FIRST_BLOOD,
                    game_time=new.game_time,
                    data={"recipient": evt.get("Recipient", "")},
                    timestamp=time.time(),
                ))

            elif event_name == "Ace":
                events.append(GameEvent(
                    event_type=GameEventType.ACE,
                    game_time=new.game_time,
                    data={"acer": evt.get("Acer", "")},
                    timestamp=time.time(),
                ))

        return events

    def _detect_item_changes(self, old: GameSnapshot, new: GameSnapshot) -> List[GameEvent]:
        """检测物品变化"""
        events = []
        old_players = {p.summoner_name: p for p in old.players}

        for new_p in new.players:
            old_p = old_players.get(new_p.summoner_name)
            if not old_p:
                continue

            old_items = {item.get("itemID") for item in old_p.items}
            new_items = {item.get("itemID") for item in new_p.items}

            added = new_items - old_items
            removed = old_items - new_items

            if added or removed:
                events.append(GameEvent(
                    event_type=GameEventType.PLAYER_ITEM_CHANGED,
                    game_time=new.game_time,
                    data={
                        "player_name": new_p.summoner_name,
                        "champion_name": new_p.champion_name,
                        "added_items": list(added),
                        "removed_items": list(removed),
                        "is_current_player": new_p.is_current_player,
                    },
                    timestamp=time.time(),
                ))

        return events

    def _detect_summoner_spell_usage(self, old: GameSnapshot,
                                       new: GameSnapshot) -> List[GameEvent]:
        """检测召唤师技能使用（通过冷却时间变化）"""
        events = []
        old_players = {p.summoner_name: p for p in old.players}

        for new_p in new.players:
            old_p = old_players.get(new_p.summoner_name)
            if not old_p:
                continue

            for i, (old_spell, new_spell) in enumerate(
                zip(old_p.summoner_spells, new_p.summoner_spells)
            ):
                old_cd = old_spell.get("cooldownRemaining", 0)
                new_cd = new_spell.get("cooldownRemaining", 0)

                # 冷却从 0 变为 >0 表示刚使用
                if old_cd <= 0 and new_cd > 1:
                    spell_name = new_spell.get("displayName", f"summoner_{i}")
                    events.append(GameEvent(
                        event_type=GameEventType.PLAYER_SUMMONER_SPELL_USED,
                        game_time=new.game_time,
                        data={
                            "player_name": new_p.summoner_name,
                            "champion_name": new_p.champion_name,
                            "spell_name": spell_name,
                            "spell_slot": i,
                            "cooldown": new_cd,
                            "team": new_p.team,
                            "is_enemy": new_p.team != new.ally_team_name,
                            "is_current_player": new_p.is_current_player,
                        },
                        timestamp=time.time(),
                    ))

        return events

    def _detect_objectives(self, old: GameSnapshot, new: GameSnapshot) -> List[GameEvent]:
        """检测中立资源事件（从 eventdata）"""
        events = []

        old_events = {self._event_key(e) for e in old.recent_events}

        for evt in new.recent_events:
            key = self._event_key(evt)
            if key in old_events:
                continue

            event_name = evt.get("EventName", "")

            if event_name == "DragonKill":
                events.append(GameEvent(
                    event_type=GameEventType.DRAGON_KILLED,
                    game_time=new.game_time,
                    data={
                        "dragon_type": evt.get("DragonType", ""),
                        "killer_name": evt.get("KillerName", ""),
                        "stolen": evt.get("Stolen", False),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "BaronKill":
                events.append(GameEvent(
                    event_type=GameEventType.BARON_KILLED,
                    game_time=new.game_time,
                    data={
                        "killer_name": evt.get("KillerName", ""),
                        "stolen": evt.get("Stolen", False),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "HeraldKill":
                events.append(GameEvent(
                    event_type=GameEventType.HERALD_KILLED,
                    game_time=new.game_time,
                    data={
                        "killer_name": evt.get("KillerName", ""),
                        "stolen": evt.get("Stolen", False),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "TurretKilled":
                events.append(GameEvent(
                    event_type=GameEventType.TOWER_DESTROYED,
                    game_time=new.game_time,
                    data={
                        "tower_name": evt.get("TurretKilled", ""),
                        "killer_name": evt.get("KillerName", ""),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "InhibKilled":
                events.append(GameEvent(
                    event_type=GameEventType.INHIBITOR_DESTROYED,
                    game_time=new.game_time,
                    data={"inhibitor": evt.get("InhibKilled", "")},
                    timestamp=time.time(),
                ))

        return events

    def _find_team(self, snapshot: GameSnapshot, name: str) -> str:
        """查找玩家所属队伍"""
        for p in snapshot.players:
            if p.summoner_name == name or p.champion_name == name:
                return p.team
        return "UNKNOWN"

    def _event_key(self, event: Dict[str, Any]) -> str:
        """生成事件唯一键（用于去重）"""
        event_name = event.get("EventName", "")
        event_time = event.get("EventTime", 0)
        # 加上一些关键字段
        key_parts = [event_name, str(event_time)]
        for field in ["KillerName", "VictimName", "DragonType", "TurretKilled"]:
            if field in event:
                key_parts.append(str(event[field]))
        return "|".join(key_parts)

    def _deduplicate(self, events: List[GameEvent]) -> List[GameEvent]:
        """事件去重"""
        result = []
        seen = set()
        for evt in events:
            key = f"{evt.event_type}|{evt.game_time}|{str(evt.data)[:100]}"
            if key not in seen:
                seen.add(key)
                result.append(evt)
        return result

    def reset(self):
        """重置状态（新游戏开始时调用）"""
        self._last_snapshot = None
        self._last_kill_time = 0.0
        self._kill_streak = 0
        self._last_kill_team = ""
        self._known_events.clear()
