"""CooldownManager锛氱鐞嗗涓墜鍔?reminder锛岀敤 Scheduler 鍋氫竴娆℃€ц鏃躲€?""
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

_SLOT_ZH = {"Q": "Q", "W": "W", "E": "E", "R": "澶ф嫑"}
_SIDE_ZH = {"enemy": "鏁屾柟", "ally": "鎴戞柟", "self": "浣犵殑"}
# 璇煶鍒悕 鈫?鏍囧噯妲戒綅
_SLOT_ALIAS = {"澶ф嫑": "R", "澶?: "R", "ULT": "R", "U": "R", "r": "R"}


def _normalize_slot(skill: str) -> str:
    """鎶婅闊?UI 浼犳潵鐨勬妧鑳藉悕褰掍竴鍖栦负 Q/W/E/R銆?""
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
        # speech_enqueue(text, event_id) 鈥?鐢?main.py 娉ㄥ叆 SpeechQueue 閫傞厤
        self._speech_enqueue = speech_enqueue
        self._remind_before = remind_before
        self._policy = policy or ReminderPolicy()
        self._scheduler = scheduler or get_scheduler()
        # callable(champion_name, side) -> float ability_haste锛涘彲閫夛紝缂虹渷 0
        self._haste_provider = haste_provider

        self._reminders: Dict[str, CooldownReminder] = {}
        self._task_ids: Dict[str, List[str]] = {}
        self._lock = threading.Lock()
        # 鍞竴鏃堕棿鍩哄噯锛?999 娓告垙鍐?gameTime銆?        # _clock_synced=True 琛ㄧず宸叉敹鍒版湁鏁?game.time_update锛?        # 鏈悓姝ユ椂绂佹鍒涘缓浠讳綍璁℃椂锛岀粷涓嶅洖閫€鐜板疄鏃堕棿銆?        self._game_time: float = 0.0
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
        cname = champ.name_zh if champ else (champion_name or "鏈煡鑻遍泟")

        slot = _normalize_slot(skill)
        if slot in ("Q", "W", "E", "R"):
            if cid is None:
                return None
            
            # 鎶€鑳界瓑绾ф帹鏂鍒欙細
            # Q/W/E锛氬缁堥粯璁?rank = 1锛堟潵婧愶細assumed_level_1锛?            # R锛氭牴鎹嫳闆勭瓑绾ф帹鏂紙鏉ユ簮锛歩nferred_from_champion_level锛?            skill_level_source = ""
            if slot == "R":
                # R 鏍规嵁鑻遍泟绛夌骇鎺ㄦ柇
                if champion_level is None:
                    # 娌℃湁鑻遍泟绛夌骇锛岄粯璁ゆ寜 1 绾у鐞嗭紙浣嗘爣璁颁负鎺ㄦ柇锛?                    rank = 1
                    skill_level_source = "inferred_from_champion_level"
                elif champion_level < 6:
                    # R 鏈В閿侊紝涓嶅惎鍔?R 鍐峰嵈璁＄畻
                    self._on_event("cooldown.r_not_unlocked", {
                        "champion": champion_name, "skill": skill,
                        "champion_level": champion_level,
                    })
                    if self._speech_enqueue:
                        try:
                            self._speech_enqueue(
                                "澶ф嫑杩樻病瑙ｉ攣鍛?,
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
                # Q/W/E锛氬缁堥粯璁?rank = 1
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
                            "璇ユ妧鑳芥病鏈夊浐瀹氬喎鍗存椂闂达紝鏃犳硶鍒涘缓璁℃椂鍣?,
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
            # 鏈悓姝ユ父鎴忔椂閽燂細涓嶅缓璁℃椂锛屼篃涓嶅洖閫€鐜板疄鏃堕棿銆?            self._on_event("cooldown.not_synced", {"champion": champion_name, "skill": skill})
            if self._speech_enqueue:
                try:
                    self._speech_enqueue("娓告垙鏃堕棿灏氭湭鍚屾锛屾殏鏃舵棤娉曡鏃躲€?,
                                         f"cooldown.not_synced.{uuid.uuid4().hex[:8]}")
                except Exception:
                    pass
            return None

        rid = uuid.uuid4().hex[:8]
        now = self._game_time  # 鍞竴鍩哄噯锛氭父鎴忓唴 gameTime
        
        # 鍒涘缓 Cast Snapshot锛堣褰曟椂鐨勭姸鎬佸揩鐓э紝鐪熸涓嶅彲鍙橈級
        # runes: 娣辨嫹璐?+ MappingProxyType锛岀‘淇濆師濮?dict 鍚庣画淇敼涓嶅奖鍝?snapshot
        immutable_runes = tuple(
            MappingProxyType(copy.deepcopy(r)) 
            for r in ()  # TODO: 浠?EnemyProfile 璇诲彇 runes 鍚庡～鍏?        )
        cast_snapshot = CooldownCastSnapshot(
            champion_name=cname or "",
            spell=display,
            cast_game_time=now,
            champion_level=0,  # TODO: 浠?EnemyProfile 璇诲彇
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
        self._schedule(r)  # 鍦ㄩ攣澶栬皟搴︼紝閬垮厤 _tick_game_clock 閲嶅叆闈為噸鍏ラ攣姝婚攣
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
        """娓呯┖鎵€鏈?reminder锛圙AME_ENDED 鏃惰皟鐢級銆?""
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
        """鍙璁块棶娲昏穬 reminder锛堜緵 Overlay 浣跨敤锛夈€?        
        杩斿洖鎵€鏈?remaining > 0 鐨?reminder锛屽寘鎷?ACTIVE 鍜?REMINDED 鐘舵€併€?        杩欐牱 Overlay 鍙互缁х画鏄剧ず鍊掕鏃讹紝鐩村埌 cooldown 鐪熸缁撴潫銆?        """
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
        """瀵规寚瀹?reminder 搴旂敤鍔ㄦ€佷慨姝ｏ紙濡?R refund锛夈€?        
        骞傜瓑锛氬悓涓€ (reminder_id, event_id) 鍙鐞嗕竴娆°€?        鍙綔鐢ㄤ簬 ACTIVE 鐘舵€佺殑 reminder銆?        adjustment_seconds > 0 鈫?ready_at 鎻愬墠銆?        """
        if not reminder_id or not event_id or adjustment_seconds <= 0:
            return False
        
        with self._lock:
            r = self._reminders.get(reminder_id)
            if r is None:
                return False
            
            # 鐢熷懡鍛ㄦ湡锛氬彧澶勭悊 ACTIVE
            if r.status != ReminderStatus.ACTIVE.value:
                return False
            
            # 骞傜瓑妫€鏌?            if event_id in r.applied_event_ids:
                return False
            
            # 搴旂敤淇
            r.dynamic_adjustment += adjustment_seconds
            r.applied_event_ids.add(event_id)
            
            # 杈圭晫鎯呭喌锛氫慨姝ｅ悗 ready_at 涓嶆棭浜庡綋鍓嶆父鎴忔椂闂?            if r.ready_at < self._game_time:
                r.dynamic_adjustment = r.estimated_cooldown  # 瀹夊叏鍏滃簳
                print("[Cooldown] adjustment capped: ready_at would be <= game_time")
        
        # 瑙﹀彂涓€娆?tick锛屾鏌ユ槸鍚﹀凡缁忓埌 remind_at/ready_at
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

    # ----- 娓告垙鍐呮椂閽熸帴鍏?-----
    def on_game_time_update(self, event) -> None:
        """EventBus: game.time_update銆傚喎鍗磋鏃堕敋瀹氭父鎴忓唴鏃堕挓銆?""
        t = event.get("game_time", 0.0) if hasattr(event, "get") else 0.0
        try:
            self.set_game_time(float(t or 0.0))
        except Exception:
            pass

    def set_game_time(self, t: float) -> None:
        """鏀跺埌鏈夋晥 game.time_update锛氭爣璁板悓姝ュ苟鎺ㄨ繘璁℃椂銆?""
        self._game_time = float(t)
        self._clock_synced = True
        self._tick_game_clock()

    def reset_clock(self) -> None:
        """GAME_ENDED锛氭竻绌哄叏閮ㄨ鏃跺苟鍥炲埌鏈悓姝ョ姸鎬併€?""
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
            except Exception as e:
                ah = 0.0
                ssh = 0.0
        print(f"[CD-Diag] handle_start: champion={p.get('champion')}, skill={p.get('skill') or p.get('spell')}, side={p.get('side', 'enemy')}, AH={ah}, SSH={ssh}")
        return self.start_reminder(
            champion_name=p.get("champion"),
            skill=p.get("skill") or p.get("spell"),
            side=p.get("side", "enemy"),
            ability_haste=ah,
            summoner_spell_haste=ssh,
        )

    # ----- 闈欐€?CD 鏌ヨ锛坈ooldown.query锛?----
    def query_cooldown(self, champion_name: Optional[str],
                       skill: str) -> dict:
        """鏌ヨ闈欐€佸熀纭€ CD锛岃繑鍥炵粨鏋勫寲缁撴灉锛堜笉鍒涘缓璁℃椂鍣級銆?""
        slot = _normalize_slot(skill)
        sp = self._spells.get_by_name(skill)
        if sp is not None and slot not in ("Q", "W", "E", "R"):
            return {
                "kind": "summoner_spell",
                "champion": None,
                "skill": sp.name_zh,
                "base_cooldown": sp.cooldown,
                "text": f"{sp.name_zh}鍩虹鍐峰嵈绾int(sp.cooldown)}绉掋€?,
            }

        champ = self._champions.get_by_name(champion_name) if champion_name else None
        cname = champ.name_zh if champ else (champion_name or "璇ヨ嫳闆?)
        if champ is None or slot not in ("Q", "W", "E", "R"):
            return {
                "kind": "unknown", "champion": champion_name, "skill": skill,
                "base_cooldown": None,
                "text": "娌℃湁鎵惧埌瀵瑰簲鐨勬妧鑳藉喎鍗存暟鎹€?,
            }
        cd1 = self._abilities.get_cooldown_by_level(champ.cid, slot, 1)
        cd5 = self._abilities.get_cooldown_by_level(champ.cid, slot, 5)
        if cd1 is None:
            return {
                "kind": "no_fixed", "champion": cname, "skill": slot,
                "base_cooldown": None,
                "text": f"{cname}鐨剓_SLOT_ZH.get(slot, slot)}娌℃湁鍥哄畾鍐峰嵈鏃堕棿銆?,
            }
        base = int(round(cd1))
        if cd5 is not None and abs(cd5 - cd1) > 0.5:
            text = (f"{cname}{_SLOT_ZH.get(slot, slot)}鍩虹鍐峰嵈绾base}绉掞紝"
                    f"闅忔妧鑳界瓑绾ч檷浣庯紝婊＄骇绾int(round(cd5))}绉掋€?)
        else:
            text = f"{cname}{_SLOT_ZH.get(slot, slot)}鍩虹鍐峰嵈绾base}绉掋€?
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
        # 鎸?champion 鍚嶅彇娑?        champ_name = p.get("champion")
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
        """娓告垙鍐呮椂閽熸帹杩涳細鎵弿 game_clock 璁℃椂锛屽埌杈?remind/ready 鐐瑰嵆瑙﹀彂銆?""
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
        # 鍞竴璁℃椂鍩哄噯锛氭父鎴忓唴 gameTime銆備笉鎸備换浣曠幇瀹炲畾鏃跺櫒锛?        # 瀹屽叏鐢?set_game_time() 鎺ㄨ繘锛涚珛鍗虫壂涓€娆″鐞嗏€滃凡鍒扮偣鈥濊竟鐣屻€?        self._tick_game_clock()

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
        # remind_before = 璺濈鍐峰嵈缁撴潫杩樻湁澶氬皯绉?        if r.side == "self":
            name = ""
        else:
            name = r.champion_name or ""
        
        # 鍙敜甯堟妧鑳斤細鎻愬墠 30 绉?鈫?"搴旇蹇ソ浜?锛涙帴杩戠粨鏉?鈫?"澶ф濂戒簡"
        if r.skill_type == "summoner_spell":
            if r.remind_before >= 20:  # 绾?30 绉掓彁閱掔偣
                return f"{side_zh}{name}{r.skill}搴旇蹇ソ浜?
            else:  # <10 绉?                return f"{side_zh}{name}{r.skill}澶ф濂戒簡"
        else:
            # 鏅€氭妧鑳斤細"澶ф濂戒簡"
            return f"{side_zh}{name}{r.skill}澶ф濂戒簡"

