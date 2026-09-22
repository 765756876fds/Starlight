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
from .calculator import CooldownCalculator
from .events import COOLDOWN_CANCELLED, COOLDOWN_REMIND, COOLDOWN_STARTED, COOLDOWN_READY
from .models import CooldownReminder, CooldownCastSnapshot, ReminderStatus
from types import MappingProxyType
import copy
from .policy import ReminderPolicy

_SLOT_ZH = {"Q": "Q", "W": "W", "E": "E", "R": "大招"}
_SIDE_ZH = {"enemy": "敌方", "ally": "我方", "self": "你的"}
# 语音别名 → 标准槽位
_SLOT_ALIAS = {"大招": "R", "大": "R", "ULT": "R", "U": "R", "r": "R"}


def _normalize_slot(skill: str) -> str:
    """把语音/UI 传来的技能名归一化为 Q/W/E/R。"""
    s = (skill or "").strip()
    if s in _SLOT_ALIAS:
        return _SLOT_ALIAS[s]
    up = s.upper()
    if up in ("Q", "W", "E", "R"):
        return up
    return up


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
        policy: Optional[ReminderPolicy] = None,
        haste_provider=None,
    ):
        self._champions = champions or ChampionCatalog()
        self._abilities = abilities or AbilityCatalog()
        self._spells = spells or SummonerSpellCatalog()
        self._on_event = on_event or (lambda code, data: None)
        # speech_enqueue(text, event_id) — 由 main.py 注入 SpeechQueue 适配
        self._speech_enqueue = speech_enqueue
        self._remind_before = remind_before
        self._policy = policy or ReminderPolicy()
        self._scheduler = scheduler or get_scheduler()
        # callable(champion_name, side) -> float ability_haste；可选，缺省 0
        self._haste_provider = haste_provider

        self._reminders: Dict[str, CooldownReminder] = {}
        self._task_ids: Dict[str, List[str]] = {}
        self._lock = threading.Lock()
        # 唯一时间基准：2999 游戏内 gameTime。
        # _clock_synced=True 表示已收到有效 game.time_update；
        # 未同步时禁止创建任何计时，绝不回退现实时间。
        self._game_time: float = 0.0
        self._clock_synced: bool = False

    # ----- public API -----
    def start_reminder(
        self,
        champion_name: Optional[str],
        skill: str,
        side: str = "enemy",
        ability_rank: Optional[int] = None,
        ability_haste: float = 0.0,
        ultimate_haste: float = 0.0,
        summoner_spell_haste: float = 0.0,
        champion_level: Optional[int] = None,
    ) -> Optional[CooldownReminder]:
        if not skill:
            return None

        champ = self._champions.get_by_name(champion_name) if champion_name else None
        cid = champ.cid if champ else None
        cname = champ.name_zh if champ else (champion_name or "未知英雄")

        slot = _normalize_slot(skill)
        if slot in ("Q", "W", "E", "R"):
            if cid is None:
                return None
            
            # 技能等级推断规则：
            # Q/W/E：始终默认 rank = 1（来源：assumed_level_1）
            # R：根据英雄等级推断（来源：inferred_from_champion_level）
            skill_level_source = ""
            if slot == "R":
                # R 根据英雄等级推断
                if champion_level is None:
                    # 没有英雄等级，默认按 1 级处理（但标记为推断）
                    rank = 1
                    skill_level_source = "inferred_from_champion_level"
                elif champion_level < 6:
                    # R 未解锁，不启动 R 冷却计算
                    self._on_event("cooldown.r_not_unlocked", {
                        "champion": champion_name, "skill": skill,
                        "champion_level": champion_level,
                    })
                    if self._speech_enqueue:
                        try:
                            self._speech_enqueue(
                                "大招还没解锁呢",
                                "cooldown.r_not_unlocked")
                        except Exception:
                            pass
                    return None
                elif champion_level <= 10:
                    rank = 1
                    skill_level_source = "inferred_from_champion_level"
                elif champion_level <= 15:
                    rank = 2
                    skill_level_source = "inferred_from_champion_level"
                else:  # >= 16
                    rank = 3
                    skill_level_source = "inferred_from_champion_level"
            else:
                # Q/W/E：始终默认 rank = 1
                rank = ability_rank or 1
                skill_level_source = "assumed_level_1"
            
            cd = self._abilities.get_cooldown_by_level(cid, slot, rank)
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
            estimated = CooldownCalculator.for_ability(
                cd, slot, ability_haste=ability_haste,
                ultimate_haste=ultimate_haste)
        else:
            sp = self._spells.get_by_name(skill)
            if sp is None:
                return None
            display = sp.name_zh
            cd = sp.cooldown
            stype = "summoner_spell"
            estimated = CooldownCalculator.for_summoner(
                cd, summoner_spell_haste=summoner_spell_haste)

        base_cd = cd
        est_cd = estimated if estimated is not None else float(cd)
        remind_before = self._policy.for_skill(stype, slot, est_cd)
        if not self._clock_synced:
            # 未同步游戏时钟：不建计时，也不回退现实时间。
            self._on_event("cooldown.not_synced", {"champion": champion_name, "skill": skill})
            if self._speech_enqueue:
                try:
                    self._speech_enqueue("游戏时间尚未同步，暂时无法计时。",
                                         f"cooldown.not_synced.{uuid.uuid4().hex[:8]}")
                except Exception:
                    pass
            return None

        rid = uuid.uuid4().hex[:8]
        now = self._game_time  # 唯一基准：游戏内 gameTime
        
        # 创建 Cast Snapshot（记录时的状态快照，真正不可变）
        # runes: 深拷贝 + MappingProxyType，确保原始 dict 后续修改不影响 snapshot
        immutable_runes = tuple(
            MappingProxyType(copy.deepcopy(r)) 
            for r in ()  # TODO: 从 EnemyProfile 读取 runes 后填入
        )
        cast_snapshot = CooldownCastSnapshot(
            champion_name=cname or "",
            spell=display,
            cast_game_time=now,
            champion_level=0,  # TODO: 从 EnemyProfile 读取
            items=(),
            runes=immutable_runes,
            ability_haste=ability_haste,
            summoner_spell_haste=summoner_spell_haste,
            ultimate_haste=ultimate_haste,
        )
        
        r = CooldownReminder(
            id=rid,
            champion_id=cid,
            champion_name=cname,
            skill=display,
            skill_type=stype,
            base_cooldown=base_cd,
            estimated_cooldown=est_cd,
            used_at=now,
            remind_before=remind_before,
            side=side,
            game_clock=True,
            skill_level_source=skill_level_source if stype == "ability" else "",
            cast_snapshot=cast_snapshot,
        )
        with self._lock:
            self._reminders[rid] = r
        self._schedule(r)  # 在锁外调度，避免 _tick_game_clock 重入非重入锁死锁
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

    def clear(self) -> int:
        """清空所有 reminder（GAME_ENDED 时调用）。"""
        with self._lock:
            reminders = list(self._reminders.values())
            task_ids = list(self._task_ids.values())
            self._reminders.clear()
            self._task_ids.clear()
        
        cancelled = 0
        for tids in task_ids:
            for tid in tids:
                try:
                    self._scheduler.cancel_once(tid)
                    cancelled += 1
                except Exception:
                    pass
        
        for r in reminders:
            r.status = ReminderStatus.EXPIRED.value
        
        self._on_event("cooldown.cleared", {"count": len(reminders)})
        print(f"[Cooldown] cleared {len(reminders)} reminders, cancelled {cancelled} tasks")
        return len(reminders)
    
    def get_active_reminders(self) -> list:
        """只读访问活跃 reminder（供 Overlay 使用）。
        
        返回所有 remaining > 0 的 reminder，包括 ACTIVE 和 REMINDED 状态。
        这样 Overlay 可以继续显示倒计时，直到 cooldown 真正结束。
        """
        with self._lock:
            return [
                {
                    "id": r.id,
                    "champion_name": r.champion_name,
                    "skill": r.skill,
                    "skill_type": r.skill_type,
                    "estimated_cooldown": r.estimated_cooldown,
                    "used_at": r.used_at,
                    "ready_at": r.ready_at,
                    "remind_at": r.remind_at,
                    "remaining": max(0, r.ready_at - self._game_time),
                    "side": r.side,
                    "status": r.status,
                }
                for r in self._reminders.values()
                if r.status in (ReminderStatus.ACTIVE.value, ReminderStatus.REMINDED.value)
            ]

    def cancel_all(self) -> int:
        with self._lock:
            ids = list(self._reminders.keys())
        n = 0
        for rid in ids:
            if self.cancel(rid):
                n += 1
        return n

    def apply_adjustment(self, reminder_id: str, adjustment_seconds: float, event_id: str) -> bool:
        """对指定 reminder 应用动态修正（如 R refund）。
        
        幂等：同一 (reminder_id, event_id) 只处理一次。
        只作用于 ACTIVE 状态的 reminder。
        adjustment_seconds > 0 → ready_at 提前。
        """
        if not reminder_id or not event_id or adjustment_seconds <= 0:
            return False
        
        with self._lock:
            r = self._reminders.get(reminder_id)
            if r is None:
                return False
            
            # 生命周期：只处理 ACTIVE
            if r.status != ReminderStatus.ACTIVE.value:
                return False
            
            # 幂等检查
            if event_id in r.applied_event_ids:
                return False
            
            # 应用修正
            r.dynamic_adjustment += adjustment_seconds
            r.applied_event_ids.add(event_id)
            
            # 边界情况：修正后 ready_at 不早于当前游戏时间
            if r.ready_at < self._game_time:
                r.dynamic_adjustment = r.estimated_cooldown  # 安全兜底
                print("[Cooldown] adjustment capped: ready_at would be <= game_time")
        
        # 触发一次 tick，检查是否已经到 remind_at/ready_at
        self._tick_game_clock()
        
        self._on_event("cooldown.adjusted", {
            "id": reminder_id,
            "event_id": event_id,
            "adjustment": adjustment_seconds,
            "new_ready_at": r.ready_at,
        })
        print(f"[Cooldown] adjusted {reminder_id} by {adjustment_seconds}s (event={event_id})")
        return True

    def list_active(self) -> List[CooldownReminder]:
        with self._lock:
            return [r for r in self._reminders.values()
                    if r.status == ReminderStatus.ACTIVE.value]

    def current_game_time(self) -> float:
        return self._game_time

    def get(self, reminder_id: str) -> Optional[CooldownReminder]:
        with self._lock:
            return self._reminders.get(reminder_id)

    # ----- 游戏内时钟接入 -----
    def on_game_time_update(self, event) -> None:
        """EventBus: game.time_update。冷却计时锚定游戏内时钟。"""
        t = event.get("game_time", 0.0) if hasattr(event, "get") else 0.0
        try:
            self.set_game_time(float(t or 0.0))
        except Exception:
            pass

    def set_game_time(self, t: float) -> None:
        """收到有效 game.time_update：标记同步并推进计时。"""
        self._game_time = float(t)
        self._clock_synced = True
        self._tick_game_clock()

    def reset_clock(self) -> None:
        """GAME_ENDED：清空全部计时并回到未同步状态。"""
        self.cancel_all()
        self._game_time = 0.0
        self._clock_synced = False

    # ----- CommandBus handlers -----
    def handle_start(self, command) -> Optional[CooldownReminder]:
        p = getattr(command, "params", command) or {}
        ah = 0.0
        ssh = 0.0
        if self._haste_provider is not None:
            try:
                result = self._haste_provider(p.get("champion"), p.get("side", "enemy"))
                if isinstance(result, tuple):
                    ah, ssh = float(result[0] or 0.0), float(result[1] or 0.0)
                else:
                    ah = float(result or 0.0)
            except Exception:
                ah = 0.0
                ssh = 0.0
        return self.start_reminder(
            champion_name=p.get("champion"),
            skill=p.get("skill") or p.get("spell"),
            side=p.get("side", "enemy"),
            ability_haste=ah,
            summoner_spell_haste=ssh,
        )

    # ----- 静态 CD 查询（cooldown.query）-----
    def query_cooldown(self, champion_name: Optional[str],
                       skill: str) -> dict:
        """查询静态基础 CD，返回结构化结果（不创建计时器）。"""
        slot = _normalize_slot(skill)
        sp = self._spells.get_by_name(skill)
        if sp is not None and slot not in ("Q", "W", "E", "R"):
            return {
                "kind": "summoner_spell",
                "champion": None,
                "skill": sp.name_zh,
                "base_cooldown": sp.cooldown,
                "text": f"{sp.name_zh}基础冷却约{int(sp.cooldown)}秒。",
            }

        champ = self._champions.get_by_name(champion_name) if champion_name else None
        cname = champ.name_zh if champ else (champion_name or "该英雄")
        if champ is None or slot not in ("Q", "W", "E", "R"):
            return {
                "kind": "unknown", "champion": champion_name, "skill": skill,
                "base_cooldown": None,
                "text": "没有找到对应的技能冷却数据。",
            }
        cd1 = self._abilities.get_cooldown_by_level(champ.cid, slot, 1)
        cd5 = self._abilities.get_cooldown_by_level(champ.cid, slot, 5)
        if cd1 is None:
            return {
                "kind": "no_fixed", "champion": cname, "skill": slot,
                "base_cooldown": None,
                "text": f"{cname}的{_SLOT_ZH.get(slot, slot)}没有固定冷却时间。",
            }
        base = int(round(cd1))
        if cd5 is not None and abs(cd5 - cd1) > 0.5:
            text = (f"{cname}{_SLOT_ZH.get(slot, slot)}基础冷却约{base}秒，"
                    f"随技能等级降低，满级约{int(round(cd5))}秒。")
        else:
            text = f"{cname}{_SLOT_ZH.get(slot, slot)}基础冷却约{base}秒。"
        return {
            "kind": "ability", "champion": cname, "skill": slot,
            "base_cooldown": cd1, "max_level_cooldown": cd5, "text": text,
        }

    def handle_query(self, command) -> dict:
        p = getattr(command, "params", command) or {}
        champion = p.get("champion")
        skill = p.get("skill") or p.get("spell") or ""
        result = self.query_cooldown(champion, skill)
        text = result.get("text") or ""
        if self._speech_enqueue and text:
            try:
                self._speech_enqueue(text, f"cooldown.query.{uuid.uuid4().hex[:8]}")
            except Exception:
                pass
        self._on_event("cooldown.queried", result)
        return result

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
    def _fire_remind(self, rem: CooldownReminder) -> None:
        self._on_event(COOLDOWN_REMIND, {
            **self._payload(rem),
            "text": self._remind_text(rem),
        })
        if self._speech_enqueue:
            try:
                self._speech_enqueue(self._remind_text(rem),
                                     f"cooldown.remind.{rem.id}")
            except Exception:
                pass

    def _pop_ready(self, rid: str) -> None:
        with self._lock:
            rem = self._reminders.pop(rid, None)
            self._task_ids.pop(rid, [])
        if rem is None:
            return
        rem.status = ReminderStatus.EXPIRED.value
        self._on_event(COOLDOWN_READY, self._payload(rem))

    def _tick_game_clock(self) -> None:
        """游戏内时钟推进：扫描 game_clock 计时，到达 remind/ready 点即触发。"""
        now = self._game_time
        if now < 0:
            return
        fired_ready = []
        with self._lock:
            for rem in list(self._reminders.values()):
                if not rem.game_clock:
                    continue
                if rem.status == ReminderStatus.ACTIVE.value and now >= rem.remind_at:
                    rem.status = ReminderStatus.REMINDED.value
                    self._fire_remind(rem)
                if rem.status in (ReminderStatus.ACTIVE.value,
                                  ReminderStatus.REMINDED.value) and now >= rem.ready_at:
                    fired_ready.append(rem.id)
        for rid in fired_ready:
            self._pop_ready(rid)

    def _schedule(self, r: CooldownReminder) -> None:
        # 唯一计时基准：游戏内 gameTime。不挂任何现实定时器，
        # 完全由 set_game_time() 推进；立即扫一次处理“已到点”边界。
        self._tick_game_clock()

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
        # remind_before = 距离冷却结束还有多少秒
        if r.side == "self":
            name = ""
        else:
            name = r.champion_name or ""
        
        # 召唤师技能：提前 30 秒 → "应该快好了"；接近结束 → "大概好了"
        if r.skill_type == "summoner_spell":
            if r.remind_before >= 20:  # 约 30 秒提醒点
                return f"{side_zh}{name}{r.skill}应该快好了"
            else:  # <10 秒
                return f"{side_zh}{name}{r.skill}大概好了"
        else:
            # 普通技能："大概好了"
            return f"{side_zh}{name}{r.skill}大概好了"
