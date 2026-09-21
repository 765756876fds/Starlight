"""CooldownCalculator — 纯确定性冷却计算。

使用游戏真实的 Haste 机制（不是自造百分比）：
  - 普通技能 (Q/W/E)：受 Ability Haste 影响
        effective = base * 100 / (100 + ability_haste)
  - 终极技能 (R)：Ability Haste 与 Ultimate Haste 叠加
        effective = base * 100 / (100 + ability_haste + ultimate_haste)
  - 召唤师技能：受 Summoner Spell Haste 影响
        effective = base * 100 / (100 + summoner_spell_haste)

任何 haste 缺失 / 为 0 / 非法值时 multiplier = 1，即回退基础 CD。
本模块不访问 LCU / DB / 网络，可单测。
"""
from __future__ import annotations

from typing import Optional, Union

Number = Union[int, float, None]


def _haste_multiplier(haste: Number) -> float:
    """100 / (100 + haste)；haste<0 或非法视为 0。"""
    try:
        h = float(haste or 0.0)
    except (TypeError, ValueError):
        h = 0.0
    if h < 0:
        h = 0.0
    return 100.0 / (100.0 + h)


def _apply(base: Number, haste: Number) -> Optional[float]:
    if base is None:
        return None
    try:
        b = float(base)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return 0.0
    return round(b * _haste_multiplier(haste), 2)


def effective_ability_cooldown(base: Number, ability_haste: Number = 0.0) -> Optional[float]:
    """普通技能 Q/W/E 的实际冷却。"""
    return _apply(base, ability_haste)


def _num(v: Number) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def effective_ultimate_cooldown(base: Number, ability_haste: Number = 0.0,
                                ultimate_haste: Number = 0.0) -> Optional[float]:
    """R 的实际冷却：Ability Haste 与 Ultimate Haste 叠加。"""
    total = _num(ability_haste) + _num(ultimate_haste)
    return _apply(base, total)


def effective_summoner_cooldown(base: Number, summoner_spell_haste: Number = 0.0) -> Optional[float]:
    """召唤师技能的实际冷却。"""
    return _apply(base, summoner_spell_haste)


class CooldownCalculator:
    """统一入口：按技能类型选择正确的 haste 属性。"""

    @staticmethod
    def for_ability(base: Number, slot: str = "",
                    ability_haste: Number = 0.0,
                    ultimate_haste: Number = 0.0) -> Optional[float]:
        slot = (slot or "").strip().upper()
        if slot == "R":
            return effective_ultimate_cooldown(base, ability_haste=ability_haste,
                                               ultimate_haste=ultimate_haste)
        return effective_ability_cooldown(base, ability_haste)

    @staticmethod
    def for_summoner(base: Number, summoner_spell_haste: Number = 0.0) -> Optional[float]:
        return effective_summoner_cooldown(base, summoner_spell_haste)


# ===== Axiom Arc (公理圆弧) Flux 退款 =====
# 版本：26.18
# 公式：refund_ratio = 10% + 0.25% × total_lethality
# 退款基于：Ultimate Ability 的 total cooldown
# 注意：26.18 官方描述未提及 0.5 秒下限（旧版本曾有，但 26.9 重做后未再提及）
#       ready_at < used_at 的安全边界由 CooldownManager.apply_adjustment() 负责

def calculate_axiom_arc_refund(
    total_ultimate_cooldown: float,
    total_lethality: float,
) -> float:
    """计算 Axiom Arc Flux 退款秒数。
    
    参数：
        total_ultimate_cooldown: R 技能总冷却时间（秒）
        total_lethality: 该英雄总 Lethality（来自 cast_snapshot.items）
    
    返回：
        退款秒数
    """
    if total_ultimate_cooldown <= 0:
        return 0.0
    
    # 公式：固定 15%（产品规则：refund = total_R_CD × 15%）
    refund_ratio = 0.15
    refund_seconds = total_ultimate_cooldown * refund_ratio
    
    return refund_seconds
