"""CooldownContext — 尽力从 GameSnapshot 抽取敌方修正信息。

设计原则（A2.1 第六节）：
  - 2999 /allgamedata 并不暴露敌方 abilityHaste / ultimateHaste / summonerSpellHaste；
  - 因此默认全部为 0，即“使用基础 CD”；
  - 若未来 snapshot 提供了这些字段，再在此处解析；
  - 任何缺失 / 解析失败都回退 0，绝不猜测、绝不因此让计时不可用。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CooldownContext:
    ability_haste: float = 0.0
    ultimate_haste: float = 0.0
    summoner_spell_haste: float = 0.0
    available: bool = False  # 是否真的解析到了修正信息

    @classmethod
    def empty(cls) -> "CooldownContext":
        return cls(0.0, 0.0, 0.0, False)

    @classmethod
    def from_snapshot(cls, snapshot, champion_name: str | None = None) -> "CooldownContext":
        """尽力解析。当前 2999 不暴露 haste，返回 empty（基础 CD）。

        保留此入口以便未来接入真实字段，调用方无需改动。
        """
        try:
            players = getattr(snapshot, "players", None) or []
            for p in players:
                name = getattr(p, "summoner_name", "") or getattr(p, "riot_id", "")
                if champion_name and name and champion_name not in name:
                    continue
                # 2999 当前不提供 abilityHaste；如未来新增字段，在此读取：
                ah = getattr(p, "ability_haste", 0.0) or 0.0
                uh = getattr(p, "ultimate_haste", 0.0) or 0.0
                ssh = getattr(p, "summoner_spell_haste", 0.0) or 0.0
                if ah or uh or ssh:
                    return cls(float(ah), float(uh), float(ssh), True)
        except Exception:
            pass
        return cls.empty()
