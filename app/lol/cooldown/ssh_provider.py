"""Summoner Spell Haste provider（敌方，确定性）。

只读 2999 enemy snapshot 中已观测到的 item / rune，只累加可确定性判断的
Summoner Spell Haste。**不使用 Ability Haste 代替 SSH**。
条件型 / 不可观测来源一律不计入（安全回退到 0）。

数据版本：League 26.18（数值需随版本复核；见 KNOWN 常量注释）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# 已知提供 Summoner Spell Haste 的来源（26.18，待真人环境复核）。
# 符文：星界洞悉 Cosmic Insight（通常 +18）
_RUNE_SSH = {
    "cosmic insight": 18.0,
    "星界洞悉": 18.0,
}
# 装备：明朗之靴 Ionian Boots of Lucency（itemID 3158，通常 +12）
_ITEM_ID_SSH = {
    3158: 12.0,
}


def summoner_spell_haste(items: List[Any], runes: List[Any]) -> Tuple[float, List[str], str]:
    """从敌方快照 items / runes 计算 SSH。

    返回 (ssh, sources, status)。
      status: "ok" 至少识别到一个确定来源；"unavailable" 无来源/数据缺失。
    任何无法确定的条件型效果都不计入。
    """
    total = 0.0
    sources: List[str] = []

    for r in runes or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("displayName") or r.get("name") or "").lower()
        for key, val in _RUNE_SSH.items():
            if key in name:
                total += val
                sources.append(f"rune:{key}")
                break

    for it in items or []:
        iid = None
        if isinstance(it, dict):
            iid = it.get("itemID")
        elif isinstance(it, (int, float)):
            iid = it
        try:
            iid = int(iid or 0)
        except (TypeError, ValueError):
            iid = 0
        if iid in _ITEM_ID_SSH:
            total += _ITEM_ID_SSH[iid]
            sources.append(f"item:{iid}")

    return float(total), sources, ("ok" if sources else "unavailable")
