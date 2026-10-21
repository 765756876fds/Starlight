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
            # Agent 启动时游戏已经在进行中：合成 GAME_STARTED
            if new_snapshot.is_game_running:
                events.append(GameEvent(
                    event_type=GameEventType.GAME_STARTED,
                    game_time=new_snapshot.game_time,
                    data={
                        "game_mode": new_snapshot.game_mode,
                        "map": new_snapshot.map_name,
                        "mid_game_start": True,
                    },
                    timestamp=time.time(),
                ))
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

            # GameStart
            if event_name == "GameStart":
                events.append(GameEvent(
                    event_type=GameEventType.GAME_STARTED,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "ChampionKill":
                killer_name = evt.get("KillerName", "")
                victim_name = evt.get("VictimName", "")
                assistants = evt.get("Assisters", [])
                riot_event_id = evt.get("EventID")  # Riot 原生 EventID

                # 查找队伍
                killer_team = self._find_team(new, killer_name)
                victim_team = self._find_team(new, victim_name)

                events.append(GameEvent(
                    event_type=GameEventType.CHAMPION_KILL,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": riot_event_id,  # Riot 原生 EventID
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

            elif event_name == "FirstBlood":
                events.append(GameEvent(
                    event_type=GameEventType.FIRST_BLOOD,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "recipient": evt.get("Recipient", ""),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "Ace":
                events.append(GameEvent(
                    event_type=GameEventType.ACE,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "acer": evt.get("Acer", ""),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "Multikill":
                # Riot 原生 Multikill 事件 - 不再自己推导多杀
                events.append(GameEvent(
                    event_type=GameEventType.MULTI_KILL,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "kill_streak": evt.get("KillStreak", 2),
                        "killer_name": evt.get("KillerName", ""),
                    },
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

            old_items = {item.get("itemID") for item in old_p.items if isinstance(item, dict)}
            new_items = {item.get("itemID") for item in new_p.items if isinstance(item, dict)}

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
                killer_name = evt.get("KillerName", "")
                real_time = float(evt.get("EventTime", new.game_time))
                events.append(GameEvent(
                    event_type=GameEventType.DRAGON_KILLED,
                    game_time=real_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "dragon_type": evt.get("DragonType", ""),
                        "killer_name": killer_name,
                        "killer_team": self._find_team(new, killer_name),
                        "stolen": evt.get("Stolen", False),
                        "game_time": real_time,
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "BaronKill":
                killer_name = evt.get("KillerName", "")
                real_time = float(evt.get("EventTime", new.game_time))
                events.append(GameEvent(
                    event_type=GameEventType.BARON_KILLED,
                    game_time=real_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "killer_name": killer_name,
                        "killer_team": self._find_team(new, killer_name),
                        "stolen": evt.get("Stolen", False),
                        "game_time": real_time,
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "HeraldKill":
                killer_name = evt.get("KillerName", "")
                events.append(GameEvent(
                    event_type=GameEventType.HERALD_KILLED,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "killer_name": killer_name,
                        "killer_team": self._find_team(new, killer_name),
                        "stolen": evt.get("Stolen", False),
                        "game_time": new.game_time,
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "TurretKilled":
                events.append(GameEvent(
                    event_type=GameEventType.TOWER_DESTROYED,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "tower_name": evt.get("TurretKilled", ""),
                        "killer_name": evt.get("KillerName", ""),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "InhibKilled":
                events.append(GameEvent(
                    event_type=GameEventType.INHIBITOR_DESTROYED,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "inhibitor": evt.get("InhibKilled", ""),
                    },
                    timestamp=time.time(),
                ))

            elif event_name == "GameEnd":
                events.append(GameEvent(
                    event_type=GameEventType.GAME_ENDED,
                    game_time=new.game_time,
                    data={
                        "riot_event_id": evt.get("EventID"),
                        "result": evt.get("Result", ""),
                    },
                    timestamp=time.time(),
                ))

        return events

    def _find_team(self, snapshot: GameSnapshot, name: str) -> str:
        """查找玩家所属队伍（P1）。

        匹配顺序：
        1) 本地 active player 身份（eventdata KillerName 对自己用 summoner 名）；
        2) playerlist 中同 summoner_name / champion_name。
        全部归一化 strip；确实无法匹配才返回 UNKNOWN，不猜测。
        """
        if not name:
            return "UNKNOWN"
        def base(x):
            return (x or "").split("#", 1)[0].strip()
        n = name.strip()
        nb = base(n)
        cp = snapshot.current_player
        if cp is not None:
            if n == (cp.summoner_name or "").strip() or n == (cp.champion_name or "").strip() \
               or nb == base(cp.summoner_name):
                return cp.team
        for p in snapshot.players:
            pn = (p.summoner_name or "").strip()
            pc = (p.champion_name or "").strip()
            if n == pn or n == pc or nb == base(pn):
                return p.team
        return "UNKNOWN"

    def _event_key(self, event: Dict[str, Any]) -> str:
        """生成事件唯一键（用于去重）

        优先使用 Riot 原生 EventID 作为唯一身份。
        如果没有 EventID，则使用 fallback 组合键。
        """
        # 优先使用 Riot 原生 EventID（注意 EventID=0 是合法的 GameStart 事件）
        event_id = event.get("EventID")
        if event_id is not None:
            return f"riot:{event_id}"

        # Fallback：没有 EventID 的事件使用组合键
        event_name = event.get("EventName", "")
        event_time = event.get("EventTime", 0)
        key_parts = [event_name, str(event_time)]
        for field in ["KillerName", "VictimName", "DragonType", "TurretKilled"]:
            if field in event:
                key_parts.append(str(event[field]))
        return "fallback:" + "|".join(key_parts)

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
        self._last_kill_team = ""
        self._known_events.clear()
