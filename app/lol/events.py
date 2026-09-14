"""
LoL 游戏事件定义

所有游戏内事件的统一数据结构。
由 StateDiff 产生，通过 EventBus 分发。

注意：事件类型常量统一使用 app.core.event_bus.EventType
本文件仅保留 GameEvent 数据类和辅助函数。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.event_bus import EventType


@dataclass
class GameEvent:
    """游戏事件基类"""
    event_type: str
    game_time: float = 0.0
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "game_time": self.game_time,
            "data": self.data,
            "timestamp": self.timestamp,
        }


# 事件类型统一使用 EventType（从 event_bus 导入）
# 这里保留 GameEventType 作为别名，向后兼容
# 新代码应直接使用 EventType
GameEventType = EventType


@dataclass
class ChampionKillEvent(GameEvent):
    """击杀事件"""
    killer_name: str = ""
    killer_team: str = ""
    victim_name: str = ""
    victim_team: str = ""
    assistants: List[str] = field(default_factory=list)
    is_first_blood: bool = False


@dataclass
class ObjectiveEvent(GameEvent):
    """中立资源事件"""
    objective_type: str = ""  # dragon / baron / herald
    dragon_type: str = ""     # fire / water / earth / air / hextech / chemtech / elder
    killer_team: str = ""
    killer_name: str = ""


@dataclass
class LevelUpEvent(GameEvent):
    """升级事件"""
    player_name: str = ""
    player_team: str = ""
    new_level: int = 0
    old_level: int = 0


@dataclass
class TowerEvent(GameEvent):
    """防御塔事件"""
    tower_name: str = ""
    team: str = ""
    lane: str = ""  # top / mid / bot
    tier: int = 0   # 1 / 2 / 3


def event_from_dict(data: Dict[str, Any]) -> GameEvent:
    """从字典创建事件"""
    event_type = data.get("event_type", GameEventType.UNKNOWN_EVENT)
    return GameEvent(
        event_type=event_type,
        game_time=data.get("game_time", 0),
        data=data.get("data", {}),
        timestamp=data.get("timestamp", 0),
    )
