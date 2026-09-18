"""CooldownReminder 数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ReminderStatus(str, Enum):
    ACTIVE = "active"
    REMINDED = "reminded"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class CooldownReminder:
    id: str
    champion_id: Optional[int]
    champion_name: Optional[str]
    skill: str                 # 'W' / 'R' / 'Flash' / 'Teleport'
    skill_type: str            # 'ability' | 'summoner_spell'
    base_cooldown: float       # 秒
    estimated_cooldown: float  # 秒（含修正）
    used_at: float             # monotonic 秒
    remind_before: float       # 提前提醒秒数
    side: str                  # 'enemy' | 'ally' | 'self'
    source: str = "USER_REPORTED"
    status: str = ReminderStatus.ACTIVE.value

    @property
    def ready_at(self) -> float:
        return self.used_at + self.estimated_cooldown

    @property
    def remind_at(self) -> float:
        return self.ready_at - self.remind_before
