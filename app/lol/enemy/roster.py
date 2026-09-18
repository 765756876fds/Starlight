"""敌方资料观察层（能力一：自动识别敌方是谁/等级/装备/召唤师技能）。

只读 GameSnapshot，不发请求、不直接 TTS、不猜隐藏数据。
敌方 Ability Haste / Ultimate Haste 2999 不直接提供 → haste 一律按 0，
haste_source 记录来源；后续如引入 item→haste 映射再扩展，不猜。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..snapshot import GameSnapshot, PlayerInfo


@dataclass(frozen=True)
class EnemyProfile:
    champion_name: str
    champion_id: int
    summoner_name: str
    team: str
    level: int
    items: tuple = field(default_factory=tuple)        # itemID 列表
    summoner_spells: tuple = field(default_factory=tuple)  # displayName 列表
    ability_haste: float = 0.0
    ultimate_haste: float = 0.0
    haste_source: str = "unavailable"   # unavailable | base_only | live_inventory


class EnemyRoster:
    """从 GameSnapshot 构建敌方资料表。纯函数、可单测。"""

    def build(self, snapshot: GameSnapshot) -> List[EnemyProfile]:
        out: List[EnemyProfile] = []
        for p in (snapshot.enemy_team or []):
            if not p.champion_name:
                continue
            spell_names = tuple(
                s.get("displayName", "") for s in (p.summoner_spells or [])
            )
            item_ids = tuple(
                int(i.get("itemID", 0)) for i in (p.items or [])
                if isinstance(i, dict) and i.get("itemID")
            )
            out.append(EnemyProfile(
                champion_name=p.champion_name,
                champion_id=int(p.champion_id or 0),
                summoner_name=p.summoner_name,
                team=p.team,
                level=int(p.level or 0),
                items=item_ids,
                summoner_spells=spell_names,
                ability_haste=0.0,        # 2999 不提供，不猜
                ultimate_haste=0.0,
                haste_source="unavailable",
            ))
        return out

    def find(self, snapshot: GameSnapshot, champion: str) -> Optional[EnemyProfile]:
        if not champion:
            return None
        c = champion.strip().lower()
        for prof in self.build(snapshot):
            if prof.champion_name.lower() == c or str(prof.champion_id) == c:
                return prof
        return None
