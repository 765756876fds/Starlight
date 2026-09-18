"""召唤师技能目录（静态表，极少变动）。

参考 league-spell-tracker / Syronss spells.py 的 CD 数值。
单位：秒。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SummonerSpell:
    sid: int
    name_en: str
    name_zh: str
    cooldown: float  # 秒


# 国服常用召唤师技能 CD（基础值，不计算 CDR）
_SPELLS: List[SummonerSpell] = [
    SummonerSpell(4, "Flash", "闪现", 300.0),
    SummonerSpell(12, "Teleport", "传送", 360.0),
    SummonerSpell(14, "Ignite", "点燃", 180.0),
    SummonerSpell(21, "Barrier", "屏障", 180.0),
    SummonerSpell(1, "Cleanse", "净化", 210.0),
    SummonerSpell(3, "Exhaust", "虚弱", 210.0),
    SummonerSpell(6, "Ghost", "疾跑", 210.0),
    SummonerSpell(7, "Heal", "治疗", 240.0),
    SummonerSpell(11, "Smite", "惩戒", 15.0),
]


# 中文名别名 → 英文名
_ZH_ALIAS: Dict[str, str] = {
    "闪现": "Flash",
    "闪": "Flash",
    "传送": "Teleport",
    "tp": "Teleport",
    "TP": "Teleport",
    "点燃": "Ignite",
    "点火": "Ignite",
    "屏障": "Barrier",
    "护盾": "Barrier",
    "净化": "Cleanse",
    "虚弱": "Exhaust",
    "疾跑": "Ghost",
    "治疗": "Heal",
    "奶": "Heal",
    "惩戒": "Smite",
    "打野刀": "Smite",
}


class SummonerSpellCatalog:
    """召唤师技能查询。"""

    def __init__(self, spells: Optional[List[SummonerSpell]] = None):
        spells = spells or _SPELLS
        self._by_id: Dict[int, SummonerSpell] = {s.sid: s for s in spells}
        self._by_en: Dict[str, SummonerSpell] = {s.name_en.lower(): s for s in spells}
        self._by_zh: Dict[str, SummonerSpell] = {}
        for s in spells:
            self._by_zh[s.name_zh] = s

    def get_by_id(self, sid: int) -> Optional[SummonerSpell]:
        return self._by_id.get(sid)

    def get_by_name(self, name: str) -> Optional[SummonerSpell]:
        if not name:
            return None
        n = name.strip()
        if n in self._by_id:
            return self._by_id[int(n)]
        low = n.lower()
        if low in self._by_en:
            return self._by_en[low]
        if n in self._by_zh:
            return self._by_zh[n]
        if n in _ZH_ALIAS and _ZH_ALIAS[n] in self._by_en:
            return self._by_en[_ZH_ALIAS[n].lower()]
        return None

    def all(self) -> List[SummonerSpell]:
        return list(self._by_id.values())
