"""OverlayModel：敌方 5 人召唤师技能倒计时浮窗模型（最小适配 LeagueAkari CD Timer）。

数据直接来自 2999 GameSnapshot.enemy_team（不依赖 PUUID、不复制 LCU player selection）：
每人 champion + position + 两个召唤师技能槽（summonerSpellOne / summonerSpellTwo）。
计时锚定游戏内 game_time（游戏暂停时不前进），UI 仅做 100ms presentation 刷新。

计时来源：
- 语音「对面X闪现了」：main.py 在 GameAlarmEngine.create_flash_alarm 成功后调用
  record_spell_cast(...)，与语音提醒解耦。
- 同一 (champion_id, slot) 重复记录 = 更新/覆盖，不叠加（去重）。

本模型不新建任何 TimerEngine；计时器只是 {key: state} 字典，剩余时间按 game_time 现算。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ...lol.data.summoner_spell_catalog import SummonerSpellCatalog


# LeagueAkari：TOP JUNGLE MIDDLE BOTTOM UTILITY
_POSITION_ORDER = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4}


# 召唤师技能变体关键词 → canonical name_zh
# 用于把 2999 的各种变体名称规范化成 Catalog 中的 canonical name
_SPELL_VARIANT_KEYWORDS = [
    ("闪现", "闪现"),
    ("引燃", "点燃"),
    ("点燃", "点燃"),
    ("传送", "传送"),
    ("疾跑", "疾跑"),
    ("幽灵疾步", "疾跑"),
    ("治疗", "治疗"),
    ("屏障", "屏障"),
    ("虚弱", "虚弱"),
    ("净化", "净化"),
    ("惩戒", "惩戒"),
]


def _normalize_spell_name(name: str, catalog) -> str:
    """把 2999 的各种变体名称规范化成 Catalog 中的 canonical name_zh。"""
    if not name:
        return ""
    
    # 先尝试直接匹配
    entry = catalog.get_by_name(name)
    if entry:
        return entry.name_zh
    
    # 尝试关键词匹配
    n_lower = name.lower()
    for keyword, canonical in _SPELL_VARIANT_KEYWORDS:
        if keyword in name or keyword.lower() in n_lower:
            entry = catalog.get_by_name(canonical)
            if entry:
                return entry.name_zh
    
    # 如果都匹配不上，返回原始名称
    return name


def _norm_position(p: str) -> str:
    """MID→MIDDLE, BOT/ADC→BOTTOM, SUP/SUPPORT→UTILITY（与 LeagueAkari 一致）。"""
    p = (p or "").upper()
    if p == "MID":
        return "MIDDLE"
    if p in ("BOT", "ADC"):
        return "BOTTOM"
    if p in ("SUP", "SUPPORT"):
        return "UTILITY"
    return p


def fmt_remaining(remaining: float) -> str:
    """LeagueAkari TimerItem.vue 的显示规则。"""
    if remaining < 0:
        return "OK"
    if remaining > 999:
        return "999"
    if remaining < 10:
        return f"{remaining:.1f}"
    return str(int(remaining))


@dataclass
class OverlaySpell:
    slot: int                      # 1 = D, 2 = F（对应 summonerSpellOne / Two）
    spell_id: int
    spell_name: str
    base_cooldown: float
    remaining: float = 0.0         # ready_at - game_time；<0 表示已转好
    active: bool = False           # 是否有正在跑的计时器


@dataclass
class OverlaySkill:
    """英雄技能（Q/W/E/R）"""
    skill: str                      # Q / W / E / R
    remaining: float = 0.0         # ready_at - game_time；<0 表示已转好
    active: bool = False           # 是否有正在跑的计时器


@dataclass
class OverlayEnemyRow:
    champion_id: int
    champion_name: str
    position: str                  # 归一化后
    order: int
    spell1: OverlaySpell
    spell2: OverlaySpell
    is_dead: bool = False          # 是否死亡
    respawn_timer: float = 0.0     # 剩余复活时间（秒）


class OverlayModel:
    """只读 GameSnapshot.enemy_team；计时状态自持，不创建任何引擎。"""

    def __init__(
        self,
        snapshot_provider: Optional[Callable[[], object]] = None,
        phase_provider: Optional[Callable[[], str]] = None,
        catalog: Optional[SummonerSpellCatalog] = None,
        cooldown_manager_provider: Optional[Callable[[], object]] = None,
    ):
        self._snapshot_provider = snapshot_provider or (lambda: None)
        self._phase_provider = phase_provider or (lambda: "None")
        self._spells = catalog or SummonerSpellCatalog()
        from app.lol.data.champion_catalog import ChampionCatalog
        self._champions = ChampionCatalog()
        self._enabled = True
        self._cooldown_manager_provider = cooldown_manager_provider or (lambda: None)
        # (champion_id, slot) -> timer state
        self._timers: Dict[tuple, dict] = {}

    # ----- 开关（供 MainWindow 复选框）-----
    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)

    def is_enabled(self) -> bool:
        return self._enabled

    # ----- 计时写入（语音记闪现走这里）-----
    def record_spell_cast(
        self,
        champion_id: int,
        champion_name: str,
        slot: int,
        spell_id: int,
        spell_name: str,
        cooldown: float,
        game_time: float,
    ) -> None:
        """记录一次敌方召唤师技能使用。同 (champion_id, slot) 重复调用=覆盖更新。"""
        key = (int(champion_id or 0), int(slot))
        start = float(game_time or 0.0)
        cd = float(cooldown or 0.0)
        self._timers[key] = {
            "start": start,
            "cooldown": cd,
            "ready_at": start + cd,
            "spell_id": int(spell_id or 0),
            "spell_name": spell_name or "",
            "champion_name": champion_name or "",
        }

    def clear(self) -> None:
        """game end / 离开 InProgress：清空计时，新局不继承。"""
        self._timers.clear()

    def timer_count(self) -> int:
        return len(self._timers)

    # ----- 渲染数据 -----
    def get_rows(self) -> List[OverlayEnemyRow]:
        """按敌方 5 人、position 排序返回；非 InProgress / 无数据返回 []。"""
        if not self._enabled:
            return []
        try:
            phase = self._phase_provider() or "None"
        except Exception:
            phase = "None"
        if phase != "InProgress":
            return []
        try:
            snap = self._snapshot_provider()
        except Exception:
            snap = None
        if snap is None:
            return []
        gt = float(getattr(snap, "game_time", 0.0) or 0.0)

        # 从 CooldownManager 读取活跃技能冷却
        active_skills: Dict[str, Dict[str, OverlaySkill]] = {}
        try:
            cm = self._cooldown_manager_provider()
            if cm is not None:
                reminders = cm.get_active_reminders()
                for r in reminders:
                    cname = r.get("champion_name", "")
                    skill = r.get("skill", "")
                    if not cname or not skill:
                        continue
                    # 用 champion_id 作为 key，避免不同中文名不匹配
                    from app.lol.data.champion_catalog import ChampionCatalog
                    champ = self._champions.get_by_name(cname)
                    cid_key = champ.cid if champ else 0
                    if cid_key not in active_skills:
                        active_skills[cid_key] = {}
                    remaining = r.get("remaining", 0.0)
                    active_skills[cid_key][skill] = OverlaySkill(
                        skill=skill,
                        remaining=max(0, remaining),
                        active=True,
                    )
        except Exception:
            pass

        rows: List[OverlayEnemyRow] = []
        for p in (getattr(snap, "enemy_team", None) or []):
            cname = getattr(p, "champion_name", "") or ""
            if not cname:
                continue
            # 用 championName → ChampionCatalog → canonical champion_id
            # 不要直接读 2999 的 champion_id（2999 不返回 championId）
            champ = self._champions.get_by_name(cname)
            cid = champ.cid if champ else 0
            canonical_cname = champ.name_zh if champ else cname
            pos = _norm_position(getattr(p, "position", ""))
            order = _POSITION_ORDER.get(pos, 99)
            spells = getattr(p, "summoner_spells", None) or []
            # 用 champion_id 匹配 active_skills
            skills_for_champ = active_skills.get(cid, {})
            # print(f"[Overlay] champion={cname} canonical_cid={cid} active_skill_keys={list(skills_for_champ.keys())}")
            s1 = self._build_spell(cid, 1, spells[0] if len(spells) > 0 else {}, gt, skills_for_champ)
            s2 = self._build_spell(cid, 2, spells[1] if len(spells) > 1 else {}, gt, skills_for_champ)

            # Q/W/E/R 已从 UI 删除（H.4.4-R13）
            sq = sw = se = sr = None

            is_dead = getattr(p, "is_dead", False) or getattr(p, "respawn_timer", 0) > 0
            respawn_timer = float(getattr(p, "respawn_timer", 0) or 0)
            rows.append(OverlayEnemyRow(cid, cname, pos, order, s1, s2, is_dead, respawn_timer))
        rows.sort(key=lambda r: (r.order,))
        return rows

    def _build_spell(self, cid: int, slot: int, spell_dict: object, gt: float, skills_for_champ: dict = None) -> OverlaySpell:
        if not isinstance(spell_dict, dict):
            spell_dict = {}
        try:
            sid = int(spell_dict.get("id", 0) or 0)
        except Exception:
            sid = 0
        # 过滤海克斯闪现（spell_id=32），不显示在悬浮窗
        if sid == 32:
            sid = 0
            spell_dict = {}
        entry = self._spells.get_by_id(sid) if sid else None
        name = entry.name_zh if entry else (spell_dict.get("displayName") or "")
        base = float(entry.cooldown) if entry else 0.0
        # 规范化 spell 名称：把 2999 displayName 转换成 canonical name_zh
        # 例如："引燃" → "点燃"，"解封的传送" → "传送"，这样才能匹配 active_skills 的 key
        canonical_name = _normalize_spell_name(name, self._spells)
        # 从 CooldownManager 的 active_skills 读取 CD（用 canonical_name 匹配）
        t = None
        if skills_for_champ and canonical_name in skills_for_champ:
            t = {"ready_at": gt + skills_for_champ[canonical_name].remaining}
        active = t is not None
        remaining = (t["ready_at"] - gt) if active else 0.0
        return OverlaySpell(slot, sid, name, base, remaining, active)

