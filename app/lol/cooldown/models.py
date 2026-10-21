"""CooldownReminder 数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


class ReminderStatus(str, Enum):
    ACTIVE = "active"
    REMINDED = "reminded"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class CooldownCastSnapshot:
    """技能记录时的状态快照（真正不可变）。
    
    保存用户记录技能那一刻的敌方状态，之后装备变化不影响已开始的 CD。
    
    注意：frozen=True 只保护字段绑定，不保护内部可变对象。
    所有集合字段都使用真正不可变结构（tuple / MappingProxyType / frozenset）。
    """
    champion_name: str
    spell: str
    cast_game_time: float
    champion_level: int
    items: tuple[int, ...] = field(default_factory=tuple)
    runes: tuple[MappingProxyType, ...] = field(default_factory=tuple)  # 每个 rune 是只读 mapping
    ability_haste: float = 0.0
    summoner_spell_haste: float = 0.0
    ultimate_haste: float = 0.0
    special_effects: tuple[str, ...] = field(default_factory=tuple)  # 预留：未来扩展


@dataclass
class CooldownReminder:
    id: str
    champion_id: int | None
    champion_name: str | None
    skill: str                 # 'W' / 'R' / 'Flash' / 'Teleport'
    skill_type: str            # 'ability' | 'summoner_spell'
    base_cooldown: float       # 秒
    estimated_cooldown: float  # 秒（初始，含静态修正）
    used_at: float             # monotonic 秒
    remind_before: float       # 提前提醒秒数
    side: str                  # 'enemy' | 'ally' | 'self'
    source: str = "USER_REPORTED"
    status: str = ReminderStatus.ACTIVE.value
    game_clock: bool = False     # True=used_at/ready_at 锚定游戏内时钟（跟随游戏暂停）
    skill_level_source: str = ""  # assumed_level_1 | inferred_from_champion_level
    cast_snapshot: CooldownCastSnapshot | None = None  # 记录时的状态快照
    dynamic_adjustment: float = 0.0  # 动态修正总秒数（>0 表示提前）
    applied_event_ids: set = field(default_factory=set)  # 已处理的事件 ID（幂等）

    @property
    def ready_at(self) -> float:
        return self.used_at + self.estimated_cooldown - self.dynamic_adjustment

    @property
    def remind_at(self) -> float:
        return self.ready_at - self.remind_before
