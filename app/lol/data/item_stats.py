"""Item Stats Catalog - 只读静态装备属性（Lethality）。

数据来源：data/lol_data/item_stats.json
版本：26.18.0

注意：
- 这是静态数据，不随对局变化
- 未知 itemID 返回 None（不是 0）
- 只提供 lethality，不提供 ability_haste（那是 item_haste.json 的职责）
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.path_resolve import lol_data_dir

_DATA_PATH = lol_data_dir() / "item_stats.json"

_cache: dict | None = None
_version: str = "unknown"


def _load() -> dict:
    global _cache, _version
    if _cache is not None:
        return _cache
    try:
        with open(_DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache = data.get("items", {})
        _version = data.get("version", "unknown")
    except Exception:
        _cache = {}
        _version = "unknown"
    return _cache


def get_item_lethality(item_id: int) -> float | None:
    """查询单个装备的 Lethality。
    
    返回：
    - float: 该装备提供的 Lethality
    - None: 未知装备（不是 0）
    """
    items = _load()
    entry = items.get(str(item_id))
    if entry is None:
        return None
    return float(entry.get("lethality", 0))


def sum_lethality(item_ids: tuple[int, ...]) -> float:
    """计算多个装备的总 Lethality。
    
    未知装备忽略（不影响总和）。
    """
    total = 0.0
    for iid in item_ids:
        leth = get_item_lethality(iid)
        if leth is not None:
            total += leth
    return total


def data_version() -> str:
    """返回数据版本。"""
    _load()
    return _version
