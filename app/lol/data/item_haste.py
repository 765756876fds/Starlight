"""item -> Ability Haste 确定性查表（数据来自 Data Dragon 描述扫描）。

只读、不联网、不猜。itemIDs 来自 2999 playerlist 敌方装备。
查不到的 item 不计入；空表/缺文件时返回 0, unavailable。
"""
from __future__ import annotations

import json
import os
from typing import Dict, Tuple

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                     "data", "lol_data", "item_haste.json")

_CACHE: Dict[int, int] = {}
_VERSION = ""


def _load() -> None:
    global _VERSION
    if _CACHE:
        return
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _VERSION = data.get("version", "")
        for iid, meta in data.get("items", {}).items():
            _CACHE[int(iid)] = int(meta.get("ah", 0))
    except Exception:
        _CACHE.clear()


def data_version() -> str:
    _load()
    return _VERSION


def sum_ability_haste(item_ids) -> Tuple[float, str]:
    """itemID 列表 -> (总 Ability Haste, source)。

    source: live_inventory | unavailable
    """
    _load()
    if not item_ids:
        return 0.0, "unavailable"
    total = 0
    found = False
    for iid in item_ids:
        try:
            ah = _CACHE.get(int(iid), 0)
        except (TypeError, ValueError):
            continue
        if ah:
            total += ah
            found = True
    return float(total), "live_inventory" if found else "unavailable"
