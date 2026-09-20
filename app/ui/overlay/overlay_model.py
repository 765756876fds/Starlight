"""OverlayModel：把敌方召唤师技能 cooldown 聚合成浮窗行（按敌人分组）。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class OverlaySkill:
    skill: str
    seconds_remaining: float
    source: str = "USER_MEMORY"


@dataclass
class OverlayEntry:
    champion_name: str
    skills: List[OverlaySkill] = field(default_factory=list)


_POSITION_ORDER = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4}


def _norm_position(p: str) -> str:
    p = (p or "").upper()
    if p == "MID":
        return "MIDDLE"
    if p in ("BOT", "ADC"):
        return "BOTTOM"
    if p in ("SUP", "SUPPORT"):
        return "UTILITY"
    return p


class OverlayModel:
    """只读 CooldownManager，不创建任何计时器。"""

    def __init__(self, cooldown_manager=None, objective_timer=None,
                 phase_provider: Optional[Callable[[], str]] = None,
                 position_provider: Optional[Callable[[str], str]] = None,
                 max_items: int = 8):
        self._cd = cooldown_manager
        self._obj = objective_timer
        self._phase_provider = phase_provider or (lambda: "None")
        self._position_provider = position_provider
        self._max = max_items
        self._enabled = True

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)

    def is_enabled(self) -> bool:
        return self._enabled

    def get_entries(self) -> List[OverlayEntry]:
        """按敌方英雄分组的召唤师技能；游戏外/无数据返回空。"""
        if not self._enabled:
            return []
        try:
            phase = self._phase_provider() or "None"
        except Exception:
            phase = "None"
        if phase not in ("InProgress",):
            return []

        now = 0.0
        try:
            now = self._cd.current_game_time() if self._cd else 0.0
        except Exception:
            now = 0.0

        grouped: Dict[str, OverlayEntry] = {}
        order: List[str] = []
        try:
            reminders = self._cd.list_active() if self._cd else []
        except Exception:
            reminders = []
        for r in reminders:
            if getattr(r, "side", None) != "enemy":
                continue
            if getattr(r, "skill_type", "") != "summoner_spell":
                continue  # 第一版只显示召唤师技能，QWER BLOCKED
            remaining = (r.used_at + r.estimated_cooldown) - now
            if remaining <= 0:
                continue
            name = r.champion_name or "?"
            if name not in grouped:
                grouped[name] = OverlayEntry(champion_name=name)
                order.append(name)
            grouped[name].skills.append(OverlaySkill(
                skill=r.skill,
                seconds_remaining=remaining,
                source=getattr(r, "source", "USER_REPORTED"),
            ))

        # 排序：有 position 则按 Akari 顺序，否则原序
        def pos_key(name: str):
            if self._position_provider:
                try:
                    return _POSITION_ORDER.get(_norm_position(self._position_provider(name)), 99)
                except Exception:
                    return 99
            return 99

        entries = [grouped[n] for n in order]
        entries.sort(key=lambda e: (pos_key(e.champion_name),))
        for e in entries:
            e.skills.sort(key=lambda s: s.seconds_remaining)
        return entries[: self._max]

    # 兼容旧测试/调用：返回扁平文字项
    def get_items(self):
        from .overlay_window import _fmt
        items = []
        for e in self.get_entries():
            for s in e.skills:
                items.append(type("I", (), {
                    "text": f"敌方 {e.champion_name} {s.skill}",
                    "seconds_remaining": s.seconds_remaining,
                    "urgent": s.seconds_remaining < 30,
                })())
        items.sort(key=lambda x: x.seconds_remaining)
        return items[: self._max]
