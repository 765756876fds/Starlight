"""英雄目录：从 data/lol_data/champions.json 加载，失败回退内置最小集。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Champion:
    cid: int
    name_en: str
    name_zh: str
    aliases: tuple = field(default_factory=tuple)


# 内置 fallback（网络失败且无缓存时）
_BUILTIN: List[Champion] = [
    Champion(157, "Yasuo", "亚索", ("风男", "疾风剑豪")),
    Champion(22, "Ashe", "艾希", ("寒冰", "冰弓")),
    Champion(99, "Lux", "拉克丝", ("光辉", "光女")),
]

DEFAULT_DATA = Path("data/lol_data/champions.json")

# 俗称 / 常见 ASR 误听 → 官方中文名（key 必须是 champions.json 里的 name）
_NAME_ALIASES: Dict[str, tuple] = {
    "墨菲特": ("石头人", "大石头", "制图人"),
    "艾希": ("寒冰", "冰弓", "寒冰射手"),
    "厄运小姐": ("女枪", "赏金", "赏金猎人"),
    "韦鲁斯": ("维鲁斯",),
    "卡莎": ("卡萨",),
}


class ChampionCatalog:
    """英雄查询，支持按 id / 中文名 / 别名。"""

    def __init__(self, champions: Optional[List[Champion]] = None):
        if champions is None:
            champions = self._load_default()
        self._by_id: Dict[int, Champion] = {c.cid: c for c in champions}
        self._by_zh: Dict[str, Champion] = {}
        for c in champions:
            self._by_zh[c.name_zh] = c
            for a in c.aliases:
                self._by_zh[a] = c
            for a in _NAME_ALIASES.get(c.name_zh, ()):
                self._by_zh[a] = c

    @staticmethod
    def _load_default() -> List[Champion]:
        try:
            p = DEFAULT_DATA
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                out = []
                for c in data:
                    out.append(Champion(
                        cid=int(c["id"]),
                        name_en=c.get("alias") or "",
                        name_zh=c.get("name") or c.get("title") or "",
                        aliases=tuple([c.get("title") or ""]),
                    ))
                print(f"[data] 加载 champions.json：{len(out)} 个英雄")
                return out
        except Exception as e:
            print(f"[data] champions.json 加载失败：{e}")
        print("[data] 使用内置 fallback（仅 3 英雄），完整数据不可用")
        return list(_BUILTIN)

    def get_by_id(self, cid: int) -> Optional[Champion]:
        return self._by_id.get(cid)

    def get_by_name(self, name: str) -> Optional[Champion]:
        if not name:
            return None
        n = name.strip()
        if n in self._by_zh:
            return self._by_zh[n]
        # 模糊：子串匹配
        for k, v in self._by_zh.items():
            if k and len(k) >= 2 and k in n:
                return v
        return None

    def all(self) -> List[Champion]:
        return list(self._by_id.values())
