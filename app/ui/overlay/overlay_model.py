"""OverlayModel：聚合 cooldown / objective 数据为 overlay 项。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class OverlayItem:
    text: str
    seconds_remaining: float
    urgent: bool  # < 30s


class OverlayModel:
    """从 CooldownManager + ObjectiveTimer 拉数据，不读敌方隐藏数据。"""

    def __init__(self, cooldown_manager=None, objective_timer=None,
                 phase_provider: Optional[Callable[[], str]] = None,
                 max_items: int = 8):
        self._cd = cooldown_manager
        self._obj = objective_timer
        self._phase_provider = phase_provider or (lambda: "None")
        self._max = max_items
        self._overlay_enabled = True

    def set_enabled(self, on: bool) -> None:
        self._overlay_enabled = bool(on)

    def is_enabled(self) -> bool:
        return self._overlay_enabled

    def get_items(self) -> List[OverlayItem]:
        if not self._overlay_enabled:
            return []
        try:
            phase = self._phase_provider() or "None"
        except Exception:
            phase = "None"
        if phase not in ("InProgress", "ChampSelect"):
            return []

        items: List[OverlayItem] = []
        now = time.monotonic()
        try:
            reminders = self._cd.list_active() if self._cd else []
        except Exception:
            reminders = []
        side_label = {"enemy": "敌方", "ally": "我方", "self": "你"}
        for r in reminders:
            remaining = r.ready_at - now
            if remaining <= 0:
                continue
            label = side_label.get(r.side, r.side)
            champ = r.champion_name or "?"
            items.append(OverlayItem(
                text=f"{label} {champ} {r.skill}",
                seconds_remaining=remaining,
                urgent=remaining < 30,
            ))
        # objective timer
        try:
            timers = self._obj.get_next_timers() if self._obj else []
        except Exception:
            timers = []
        for t in timers:
            items.append(OverlayItem(
                text=f"{t['name']}刷新",
                seconds_remaining=float(t["seconds_remaining"]),
                urgent=float(t["seconds_remaining"]) < 30,
            ))
        items.sort(key=lambda x: x.seconds_remaining)
        return items[: self._max]
