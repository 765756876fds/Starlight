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
# 装备：明朗之靴 Ionian Boots of Lucidity（itemID 3158，通常 +12）
_ITEM_ID_SSH = {
    3158: 12.0,
}

# 装备名称 → SSH（用于 2999 不返回 itemID 时的 fallback）
_ITEM_NAME_SSH = {
    "明朗之靴": 12.0,
    "iorian boots of lucidity": 12.0,
    "猩红明朗": 12.0,  # 猩红明朗之靴，也是 SSH 装备
}

# 启迪系 rune tree ID
_INSPIRATION_TREE_IDS = {8400}


def _has_inspiration_runes(runes: List[Any]) -> bool:
    """检查是否主系或副系是启迪（Inspiration）。"""
    for r in runes or []:
        if not isinstance(r, dict):
            continue
        # 检查 rune tree ID
        tree_id = r.get("primaryRuneTree") or r.get("runeTree") or r.get("treeId")
        if tree_id:
            try:
                if int(tree_id) in _INSPIRATION_TREE_IDS:
                    return True
            except (TypeError, ValueError):
                pass
        # 检查 displayName
        name = str(r.get("displayName") or r.get("name") or "")
        if "启迪" in name or "Inspiration" in name or "inspiration" in name.lower():
            return True
    return False


def summoner_spell_haste(items: List[Any], runes: List[Any]) -> Tuple[float, List[str], str]:
    """从敌方快照 items / runes 计算 SSH。

    返回 (ssh, sources, status)。
      status: "ok" 至少识别到一个确定来源；"unavailable" 无来源/数据缺失。
      星界洞悉是启发式假设：启迪系 → 假设拥有星界洞悉。
      source 标记为 cosmic_insight_assumed。
    """
    total = 0.0
    sources: List[str] = []

    # 1. 直接检测星界洞悉符文
    cosmic_detected = False
    for r in runes or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("displayName") or r.get("name") or "").lower()
        for key, val in _RUNE_SSH.items():
            if key in name:
                total += val
                sources.append(f"rune:{key}")
                cosmic_detected = True
                break

    # 2. 启发式：启迪系 → 假设星界洞悉（仅在未直接检测到时）
    if not cosmic_detected and _has_inspiration_runes(runes):
        total += 18.0
        sources.append("rune:cosmic_insight_assumed")

    # 3. 装备：明朗之靴
    for it in items or []:
        iid = None
        item_name = ""
        if isinstance(it, dict):
            iid = it.get("itemID") or it.get("id")
            item_name = str(it.get("displayName") or it.get("name") or "")
        elif isinstance(it, (int, float)):
            iid = it
        try:
            iid = int(iid or 0)
        except (TypeError, ValueError):
            iid = 0
        
        # 优先用 itemID 匹配
        if iid in _ITEM_ID_SSH:
            total += _ITEM_ID_SSH[iid]
            sources.append(f"item:{iid}")
            continue
        
        # fallback：用装备名称匹配（2999 不返回 itemID 时）
        if item_name:
            for name_key, val in _ITEM_NAME_SSH.items():
                if name_key.lower() in item_name.lower():
                    total += val
                    sources.append(f"item:{name_key}")
                    break
    
    return float(total), sources, ("ok" if sources else "unavailable")
