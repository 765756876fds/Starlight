"""技能目录：从 data/lol_data/abilities.json 加载，失败回退内置最小集。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Ability:
    champion_id: int
    slot: str  # Q/W/E/R
    name: str
    cooldown: float  # 1 级 CD（兼容）
    cooldown_by_level: tuple = field(default_factory=tuple)  # 完整数组


_BUILTIN: list[Ability] = [
    Ability(157, "Q", "Steel Tempest", 4.0, (4.0, 4.0, 4.0, 4.0, 4.0, 4.0)),
    Ability(157, "W", "Wind Wall", 25.0, (25.0, 23.0, 21.0, 19.0, 17.0, 15.0)),
    Ability(157, "E", "Sweeping Blade", 0.5, (0.5, 0.4, 0.3, 0.2, 0.1, 0.1)),
    Ability(157, "R", "Last Breath", 70.0, (70.0, 50.0, 30.0, 30.0, 30.0, 30.0)),
    Ability(22, "Q", "Ranger's Focus", 0.0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
    Ability(22, "W", "Volley", 18.0, (18.0, 14.5, 11.0, 7.5, 4.0, 4.0)),
    Ability(22, "E", "Hawkshot", 5.0, (5.0, 5.0, 5.0, 5.0, 5.0, 5.0)),
    Ability(22, "R", "Enchanted Crystal Arrow", 100.0, (100.0, 80.0, 60.0, 60.0, 60.0, 60.0)),
    Ability(99, "Q", "Light Binding", 10.0, (10.0, 10.0, 10.0, 10.0, 10.0, 10.0)),
    Ability(99, "W", "Prismatic Barrier", 12.0, (12.0, 11.5, 11.0, 10.5, 10.0, 9.5)),
    Ability(99, "E", "Lucent Singularity", 10.0, (10.0, 9.5, 9.0, 8.5, 8.0, 8.0)),
    Ability(99, "R", "Final Spark", 60.0, (60.0, 50.0, 40.0, 40.0, 40.0, 30.0)),
]

from app.core.path_resolve import PROJECT_ROOT

DEFAULT_DATA = PROJECT_ROOT / "data" / "lol_data" / "abilities.json"


class AbilityCatalog:
    def __init__(self, abilities: list[Ability] | None = None):
        if abilities is None:
            abilities = self._load_default()
        self._key: dict[tuple, Ability] = {
            (a.champion_id, a.slot.upper()): a for a in abilities
        }

    @staticmethod
    def _load_default() -> list[Ability]:
        try:
            p = DEFAULT_DATA
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                out = []
                for a in data:
                    arr = a.get("cooldown_by_level") or [float(a.get("cooldown") or 0.0)]
                    out.append(Ability(
                        champion_id=int(a["champion_id"]),
                        slot=a["slot"].upper(),
                        name=a.get("name") or "",
                        cooldown=float(a.get("cooldown") or 0.0),
                        cooldown_by_level=tuple(float(x) for x in arr),
                    ))
                print(f"[data] 加载 abilities.json：{len(out)} 条")
                return out
        except Exception as e:
            print(f"[data] abilities.json 加载失败：{e}")
        return list(_BUILTIN)

    def get_ability(self, champion_id: int, slot: str) -> Ability | None:
        return self._key.get((champion_id, (slot or "").upper()))

    def get_cooldown(self, champion_id: int, slot: str) -> float | None:
        """1 级 CD；CD<=0（无固定 CD，如艾希 Q）返回 None。"""
        a = self.get_ability(champion_id, slot)
        if a is None:
            return None
        return a.cooldown if a.cooldown > 0 else None

    def get_cooldown_by_level(self, champion_id: int, slot: str,
                               level: int = 1) -> float | None:
        """指定等级（1-6）的 CD；越界 clamp。CD<=0 返回 None。"""
        a = self.get_ability(champion_id, slot)
        if a is None:
            return None
        if not a.cooldown_by_level:
            return a.cooldown if a.cooldown > 0 else None
        idx = max(0, min(len(a.cooldown_by_level) - 1, int(level) - 1))
        v = a.cooldown_by_level[idx]
        return v if v > 0 else None
