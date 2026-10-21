"""敌方资料观察层（能力一：自动识别敌方是谁/等级/装备/召唤师技能）。

只读 GameSnapshot，不发请求、不直接 TTS、不猜隐藏数据。
敌方 Ability Haste / Ultimate Haste 2999 不直接提供 → haste 一律按 0，
haste_source 记录来源；后续如引入 item→haste 映射再扩展，不猜。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..snapshot import GameSnapshot


@dataclass(frozen=True)
class EnemyProfile:
    champion_name: str
    champion_id: int
    summoner_name: str
    team: str
    level: int
    items: tuple = field(default_factory=tuple)        # 完整 item dict 列表（含 displayName）
    summoner_spells: tuple = field(default_factory=tuple)  # displayName 列表
    runes: tuple = field(default_factory=tuple)       # 符文原始数据
    ability_haste: float = 0.0
    ultimate_haste: float = 0.0
    haste_source: str = "unavailable"   # unavailable | base_only | live_inventory


class EnemyRoster:
    """从 GameSnapshot 构建敌方资料表。纯函数、可单测。"""

    def build(self, snapshot: GameSnapshot) -> list[EnemyProfile]:
        out: list[EnemyProfile] = []
        for p in (snapshot.enemy_team or []):
            if not p.champion_name:
                continue
            spell_names = tuple(
                s.get("displayName", "") for s in (p.summoner_spells or [])
            )
            # 保存完整的 item dict（含 displayName），而不是只提取 itemID
            # 因为 2999 不返回 itemID，只有 displayName
            item_dicts = tuple(
                i for i in (p.items or [])
                if isinstance(i, dict)
            )
            out.append(EnemyProfile(
                champion_name=p.champion_name,
                champion_id=int(p.champion_id or 0),
                summoner_name=p.summoner_name,
                team=p.team,
                level=int(p.level or 0),
                items=item_dicts,
                summoner_spells=spell_names,
                runes=tuple(p.runes or []),
                ability_haste=0.0,        # 2999 不提供，不猜
                ultimate_haste=0.0,
                haste_source="unavailable",
            ))
        return out

    def find(self, snapshot: GameSnapshot, champion: str) -> EnemyProfile | None:
        if not champion:
            return None
        from ..data.champion_catalog import ChampionCatalog
        catalog = ChampionCatalog()
        
        # 先尝试解析传入的 champion 名称为 canonical champion
        queried_champ = catalog.get_by_name(champion)
        queried_cid = queried_champ.cid if queried_champ else None
        
        c = champion.strip().lower()
        for prof in self.build(snapshot):
            # 直接匹配名称或 ID
            if prof.champion_name.lower() == c or str(prof.champion_id) == c:
                return prof
            
            # 用 ChampionCatalog 解析 prof 的名称
            prof_champ = catalog.get_by_name(prof.champion_name)
            prof_cid = prof_champ.cid if prof_champ else None
            
            # 用 canonical champion_id 匹配
            if queried_cid is not None and prof_cid == queried_cid:
                return prof
            
            # 检查传入的 champion 是否是 prof champion 的别名
            # 例如：传入 "离群之刺"，prof.champion_name = "阿卡丽"
            if prof_champ and hasattr(prof_champ, 'aliases'):
                aliases = prof_champ.aliases or []
                if champion in aliases:
                    return prof
        
        return None
