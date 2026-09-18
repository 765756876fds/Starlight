"""ReminderPolicy — 统一提醒阈值。

避免把“提前几秒提醒”散落在多处硬编码。
  - Q/W/E（小技能）：默认提前 5 秒
  - R（大招）：默认提前 15 秒
  - 召唤师技能：默认提前 15 秒
可由配置覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReminderPolicy:
    small_remind_before: float = 5.0      # Q/W/E
    ultimate_remind_before: float = 15.0    # R
    spell_remind_before: float = 15.0      # Flash/Ignite/...

    def for_skill(self, skill_type: str, slot: str = "") -> float:
        if skill_type == "summoner_spell":
            return self.spell_remind_before
        if (slot or "").strip().upper() == "R":
            return self.ultimate_remind_before
        return self.small_remind_before

    @classmethod
    def from_config(cls, cfg: dict | None) -> "ReminderPolicy":
        cfg = cfg or {}
        try:
            return cls(
                small_remind_before=float(cfg.get("small_remind_before", 5.0)),
                ultimate_remind_before=float(cfg.get("ultimate_remind_before", 15.0)),
                spell_remind_before=float(cfg.get("spell_remind_before", 15.0)),
            )
        except (TypeError, ValueError):
            return cls()
