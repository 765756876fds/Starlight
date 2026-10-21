"""一键连招数据模型：Combo / ComboStep / ChampionCombos 及 YAML dict 解析。

字段含义严格按施工指令：
- trigger: 触发键（主键区顶部数字键或字母，大小写不敏感，配置统一小写）
- sequence: 按键序列，按顺序执行
- delay_after_ms: 执行完这个键后等待多少毫秒再执行下一个
- note: 中文说明，UI 展示用
- champion_id: LoL 英雄 ID（与 Ongoing 服务比对）
- aliases: 搜索用别名（中英文、拼音都可以）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ComboStep:
    key: str
    delay_after_ms: int = 0

    def to_dict(self) -> dict:
        return {"key": self.key, "delay_after_ms": int(self.delay_after_ms)}


@dataclass
class Combo:
    trigger: str
    note: str = ""
    sequence: List[ComboStep] = field(default_factory=list)
    cooldown_ms: int = 1000  # 同一连招触发冷却（毫秒），防连按误触

    @property
    def summary(self) -> str:
        """UI 展示用：'3→W→W'"""
        return "→".join(s.key.upper() for s in self.sequence)

    def to_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "note": self.note,
            "sequence": [s.to_dict() for s in self.sequence],
            "cooldown_ms": int(self.cooldown_ms),
        }


@dataclass
class ChampionCombos:
    name: str
    champion_id: int
    aliases: List[str] = field(default_factory=list)
    combos: List[Combo] = field(default_factory=list)

    def find_by_trigger(self, key: str) -> Optional[Combo]:
        """按触发键查连招（大小写不敏感）。"""
        k = (key or "").strip().lower()
        if not k:
            return None
        for c in self.combos:
            if (c.trigger or "").strip().lower() == k:
                return c
        return None

    def to_dict(self) -> dict:
        return {
            "champion_id": int(self.champion_id),
            "aliases": list(self.aliases),
            "combos": [c.to_dict() for c in self.combos],
        }


# ---------- YAML dict → dataclass 解析（宽松防御，坏条目跳过不崩） ----------


def parse_step(d: dict) -> Optional[ComboStep]:
    key = (d or {}).get("key")
    if not key:
        return None
    try:
        delay = int(d.get("delay_after_ms", 0) or 0)
    except (TypeError, ValueError):
        delay = 0
    return ComboStep(key=str(key).strip().lower(), delay_after_ms=delay)


def parse_combo(d: dict) -> Optional[Combo]:
    trigger = (d or {}).get("trigger")
    if not trigger:
        return None
    seq_raw = d.get("sequence") or []
    steps: List[ComboStep] = []
    for s in seq_raw:
        st = parse_step(s) if isinstance(s, dict) else None
        if st is not None:
            steps.append(st)
    if not steps:
        return None
    try:
        cooldown_ms = int(d.get("cooldown_ms", 1000) or 1000)
    except (TypeError, ValueError):
        cooldown_ms = 1000
    return Combo(
        trigger=str(trigger).strip().lower(),
        note=str(d.get("note", "") or ""),
        sequence=steps,
        cooldown_ms=cooldown_ms,
    )


def parse_champion(name: str, d: dict) -> Optional[ChampionCombos]:
    """champion 条目 → ChampionCombos；结构损坏时返回 None（跳过不崩）。"""
    if not isinstance(d, dict):
        return None
    try:
        cid = int(d.get("champion_id") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid <= 0:
        return None
    combos: List[Combo] = []
    for c in (d.get("combos") or []):
        cb = parse_combo(c) if isinstance(c, dict) else None
        if cb is not None:
            combos.append(cb)
    return ChampionCombos(
        name=str(name or ""),
        champion_id=cid,
        aliases=[str(a) for a in (d.get("aliases") or []) if a],
        combos=combos,
    )
