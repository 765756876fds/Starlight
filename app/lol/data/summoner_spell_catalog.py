"""召唤师技能目录（版本化静态表）。

数据来自 data/lol_data/summoner_spells.json（patch/version 可追踪）；
加载失败时回退到内置列表。单位：秒。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.path_resolve import PROJECT_ROOT

_SPELLS_PATH = PROJECT_ROOT / "data" / "lol_data" / "summoner_spells.json"


@dataclass(frozen=True)
class SummonerSpell:
    sid: int
    name_en: str
    name_zh: str
    cooldown: float  # 秒


# 内置 fallback（与 JSON 保持一致；JSON 不可读时使用）
# 版本：26.18
_BUILTIN: list[SummonerSpell] = [
    SummonerSpell(4, "Flash", "闪现", 300.0),
    SummonerSpell(12, "Teleport", "传送", 300.0),  # 旧版360s，当前版本改为300s
    SummonerSpell(14, "Ignite", "点燃", 180.0),
    SummonerSpell(21, "Barrier", "屏障", 180.0),
    SummonerSpell(1, "Cleanse", "净化", 210.0),
    SummonerSpell(3, "Exhaust", "虚弱", 210.0),
    SummonerSpell(6, "Ghost", "疾跑", 210.0),
    SummonerSpell(7, "Heal", "治疗", 240.0),
    SummonerSpell(11, "Smite", "惩戒", 15.0),
    SummonerSpell(13, "Clarity", "清晰术", 240.0),
    SummonerSpell(32, "Mark", "雪球", 80.0),
]


def _load_spells() -> tuple[list[SummonerSpell], str, str]:
    """(spells, data_version, patch)"""
    try:
        if _SPELLS_PATH.exists():
            data = json.loads(_SPELLS_PATH.read_text(encoding="utf-8"))
            spells = [
                SummonerSpell(int(s["sid"]), s["name_en"], s["name_zh"],
                              float(s["cooldown"]))
                for s in data.get("spells", [])
            ]
            if spells:
                return spells, data.get("data_version", "unknown"), data.get("patch", "unknown")
    except Exception as e:
        print(f"[summoner_spell_catalog] 数据加载失败，使用内置表: {e}")
    return _BUILTIN, "builtin", "builtin"


# 中文名别名 → 英文名
_ZH_ALIAS: dict[str, str] = {
    "闪现": "Flash", "闪": "Flash",
    "传送": "Teleport", "tp": "Teleport", "TP": "Teleport",
    "点燃": "Ignite", "点火": "Ignite", "引燃": "Ignite",
    "屏障": "Barrier", "护盾": "Barrier",
    "净化": "Cleanse", "虚弱": "Exhaust",
    "疾跑": "Ghost", "幽灵疾步": "Ghost", "治疗": "Heal", "奶": "Heal",
    "惩戒": "Smite", "打野刀": "Smite",
}


class SummonerSpellCatalog:
    """召唤师技能查询。"""

    def __init__(self, spells: list[SummonerSpell] | None = None):
        if spells is None:
            spells, self.data_version, self.patch = _load_spells()
        else:
            self.data_version, self.patch = "injected", "injected"
        self._by_id: dict[int, SummonerSpell] = {s.sid: s for s in spells}
        self._by_en: dict[str, SummonerSpell] = {s.name_en.lower(): s for s in spells}
        self._by_zh: dict[str, SummonerSpell] = {}
        for s in spells:
            self._by_zh[s.name_zh] = s

    def get_by_id(self, sid: int) -> SummonerSpell | None:
        return self._by_id.get(sid)

    def get_by_name(self, name: str) -> SummonerSpell | None:
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
        if n in _ZH_ALIAS and _ZH_ALIAS[n].lower() in self._by_en:
            return self._by_en[_ZH_ALIAS[n].lower()]
        return None

    def all(self) -> list[SummonerSpell]:
        return list(self._by_id.values())
