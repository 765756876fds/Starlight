"""CooldownManager：管理多个手动 reminder，用 Scheduler 做一次性计时。"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from ...core.scheduler import get_scheduler
from ..data.ability_catalog import AbilityCatalog
from ..data.champion_catalog import ChampionCatalog
from ..data.summoner_spell_catalog import SummonerSpellCatalog
from .events import COOLDOWN_CANCELLED, COOLDOWN_REMIND, COOLDOWN_STARTED, COOLDOWN_READY
from .models import CooldownReminder, ReminderStatus

_SLOT_ZH = {"Q": "Q", "W": "W", "E": "E", "R": "大招"}
_SIDE_ZH = {"enemy": "敌方", "ally": "我方", "self": "你的"}


class CooldownManager:
    def __init__(
        self,
        champions: Optional[ChampionCatalog] = None,
        abilities: Optional[AbilityCatalog] = None,
        spells: Optional[SummonerSpellCatalog] = None,
        on_event: Optional[Callable[[str, dict], None]] = None,
        speech_enqueue: Optional[Callable[[str, str], None]] = None,
        remind_before: float = 5.0,
        scheduler=None,
    ):
        self._champions = champions or ChampionCatalog()
        self._abilities = abilities or AbilityCatalog()
        self._spells = spells or SummonerSpellCatalog()
        self._on_event = on_event or (lambda code, data: None)
        # speech_enqueue(text, event_id) — 由 main.py 注入 SpeechQueue 适配
        self._speech_enqueue = speech_enqueue
        self._remind_before = remind_before
        self._scheduler = scheduler or get_scheduler()

        self._reminders: Dict[str, CooldownReminder] = {}
        self._task_ids: Dict[str, List[str]] = {}
        self._lock = threading.Lock()

    # ----- public API -----
    def start_reminder(
        self,
        champion_name: Optional[str],
        skill: str,
        side: str = "enemy",
    ) -> Optional[CooldownReminder]:
        if not skill:
            return None

        champ = self._champions.get_by_name(champion_name) if champion_name else None
        cid = champ.cid if champ else None
        cname = champ.name_zh if champ else (champion_name or "未知英雄")

        slot = skill.strip().upper()
        if slot in ("Q", "W", "E", "R"):
            if cid is None:
                return None
            cd = self._abilities.get_cooldown(cid, slot)
            if cd is None:
                self._on_event("cooldown.invalid_skill", {
                    "champion": champion_name, "skill": skill,
                    "reason": "no_fixed_cooldown",
                })
                if self._speech_enqueue:
                    try:
                        self._speech_enqueue(
                            "该技能没有固定冷却时间，无法创建计时器",
                            "cooldown.invalid_skill")
                    except Exception:
                        pass
                return None
            display = _SLOT_ZH.get(slot, slot)
            stype = "ability"
        else:
            sp = self._spells.get_by_name(skill)
            if sp is None:
                return None
            display = sp.name_zh
            cd = sp.cooldown
            stype = "summoner_spell"

        rid = uuid.uuid4().hex[:8]
        now = time.monotonic()
        r = CooldownReminder(
            id=rid,
            champion_id=cid,
            champion_name=cname,
            skill=display,
            skill_type=stype,
            base_cooldown=cd,
            estimated_cooldown=cd,
            used_at=now,
            remind_before=self._remind_before,
            side=side,
        )
        with self._lock:
            self._reminders[rid] = r
            self._schedule(r)
        self._on_event(COOLDOWN_STARTED, {"id": rid, **self._payload(r)})
        return r

    def cancel(self, reminder_id: str) -> bool:
        with self._lock:
            r = self._reminders.pop(reminder_id, None)
            tids = self._task_ids.pop(reminder_id, [])
        if r is None:
            return False
        for tid in tids:
            try:
                self._scheduler.cancel_once(tid)
            except Exception:
                pass
        self._on_event(COOLDOWN_CANCELLED, {"id": reminder_id})
        return True

    def cancel_all(self) -> int:
        with self._lock:
            ids = list(self._reminders.keys())
        n = 0
        for rid in ids:
            if self.cancel(rid):
                n += 1
        return n

    def list_active(self) -> List[CooldownReminder]:
        with self._lock:
            return [r for r in self._reminders.values()
                    if r.status == ReminderStatus.ACTIVE.value]

    def get(self, reminder_id: str) -> Optional[CooldownReminder]:
        with self._lock:
            return self._reminders.get(reminder_id)

    # ----- CommandBus handlers -----
    def handle_start(self, command) -> Optional[CooldownReminder]:
        p = getattr(command, "params", command) or {}
        return self.start_reminder(
            champion_name=p.get("champion"),
            skill=p.get("skill") or p.get("spell"),
            side=p.get("side", "enemy"),
        )

    def handle_cancel(self, command) -> bool:
        p = getattr(command, "params", command) or {}
        # 按 champion 名取消
        champ_name = p.get("champion")
        with self._lock:
            candidates = [r.id for r in self._reminders.values()
                          if (not champ_name) or (r.champion_name == champ_name)]
        n = 0
        for rid in candidates:
            if self.cancel(rid):
                n += 1
        return n > 0

    def handle_cancel_all(self, command=None) -> int:
        return self.cancel_all()

    def handle_list(self, command=None) -> List[dict]:
        return [self._payload(r) for r in self.list_active()]

    # ----- internals -----
    def _schedule(self, r: CooldownReminder) -> None:
        now = time.monotonic()
        rid = r.id
        tids: List[str] = []

        def _remind_cb():
            with self._lock:
                rem = self._reminders.get(rid)
                if rem is None or rem.status != ReminderStatus.ACTIVE.value:
                    return
                rem.status = ReminderStatus.REMINDED.value
            self._on_event(COOLDOWN_REMIND, {
                **self._payload(rem),
                "text": self._remind_text(rem),
            })
            if self._speech_enqueue:
                try:
                    self._speech_enqueue(self._remind_text(rem),
                                         f"cooldown.remind.{rid}")
                except Exception:
                    pass

        def _ready_cb():
            with self._lock:
                rem = self._reminders.pop(rid, None)
                tids = self._task_ids.pop(rid, [])
            if rem is None:
                return
            rem.status = ReminderStatus.EXPIRED.value
            self._on_event(COOLDOWN_READY, self._payload(rem))

        if r.remind_before > 0:
            delay = max(0.0, r.remind_at - now)
            tids.append(self._scheduler.schedule_once(
                delay, _remind_cb, task_id=f"cd-remind-{r.id}"))

        ready_delay = max(0.0, r.ready_at - now)
        tids.append(self._scheduler.schedule_once(
            ready_delay, _ready_cb, task_id=f"cd-ready-{r.id}"))
        self._task_ids[r.id] = tids

    def _payload(self, r: CooldownReminder) -> dict:
        return {
            "id": r.id,
            "champion_id": r.champion_id,
            "champion_name": r.champion_name,
            "skill": r.skill,
            "skill_type": r.skill_type,
            "side": r.side,
            "estimated_cooldown": r.estimated_cooldown,
            "ready_at": r.ready_at,
        }

    @staticmethod
    def _remind_text(r: CooldownReminder) -> str:
        side_zh = _SIDE_ZH.get(r.side, "")
        if r.side == "self":
            return f"你的{r.skill}即将转好"
        name = r.champion_name or ""
        return f"{side_zh}{name}{r.skill}即将转好"
