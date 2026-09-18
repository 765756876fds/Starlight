"""中立资源规则数据层（版本化）。

把散落在业务代码里的刷新时间表抽到数据文件：
  data/lol_data/objectives.json
未来 patch 更新只改 JSON，不改业务代码。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_PATH = Path("data/lol_data/objectives.json")


@dataclass(frozen=True)
class ObjectiveRule:
    type: str
    name_zh: str
    enabled: bool
    first_spawn: Optional[float]      # 首次刷新（游戏内秒）；None=由事件触发
    respawn: Optional[float]          # 击杀后刷新间隔（秒）；None=不刷新
    one_per_game: bool
    despawn: Optional[float] = None  # 离场时间（游戏内秒）
    leads: tuple = field(default_factory=tuple)  # 提前提醒秒数
    trigger: str = ""                 # 如 after_dragon_soul
    patch: str = ""
    mode: str = "classic"


@dataclass(frozen=True)
class ObjectiveRuleset:
    patch: str
    data_version: str
    source: str
    rules: Dict[str, ObjectiveRule]  # by type

    def get(self, objective_type: str) -> Optional[ObjectiveRule]:
        return self.rules.get(objective_type)

    def enabled_types(self) -> List[ObjectiveRule]:
        return [r for r in self.rules.values() if r.enabled]


_FALLBACK: Dict[str, ObjectiveRule] = {
    "void_grubs": ObjectiveRule("void_grubs", "巢虫", True, 480.0, None, True, 885.0, (60.0,)),
    "dragon": ObjectiveRule("dragon", "小龙", True, 300.0, 300.0, False, None, (60.0, 30.0)),
    "baron": ObjectiveRule("baron", "大龙", True, 1200.0, 360.0, False, None, (60.0, 30.0)),
    "elder": ObjectiveRule("elder", "远古龙", True, None, 360.0, False, None, (60.0, 30.0),
                           trigger="after_dragon_soul"),
}


def load_ruleset(path: Path = DEFAULT_PATH, mode: str = "classic") -> ObjectiveRuleset:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            patch = data.get("patch", "unknown")
            dv = data.get("data_version", "unknown")
            src = data.get("source", "")
            modes = data.get("modes", {})
            rules_list = modes.get(mode, {}).get("objectives", [])
            rules: Dict[str, ObjectiveRule] = {}
            for r in rules_list:
                rules[r["type"]] = ObjectiveRule(
                    type=r["type"],
                    name_zh=r.get("name_zh", r["type"]),
                    enabled=bool(r.get("enabled", True)),
                    first_spawn=r.get("first_spawn"),
                    respawn=r.get("respawn"),
                    one_per_game=bool(r.get("one_per_game", False)),
                    despawn=r.get("despawn"),
                    leads=tuple(r.get("leads", [])),
                    trigger=r.get("trigger", ""),
                    patch=patch,
                    mode=mode,
                )
            if rules:
                return ObjectiveRuleset(patch, dv, src, rules)
    except Exception as e:
        print(f"[objectives] 规则加载失败，使用内置 fallback: {e}")
    return ObjectiveRuleset("fallback", "builtin", "builtin", dict(_FALLBACK))
