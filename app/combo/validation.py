"""一键连招校验规则（UI 保存前使用；只读，不修改执行逻辑）。

触发键：字母 a-z / 数字 0-9（按键捕获来源；QWERDF 在 UI 层弹警告，校验层允许）
序列按键：QWERDF + 1-7 + ASB（列表点选，保持一期范围）
延迟：≥ 0 的整数（0 表示连按）
序列：至少 1 步
备注：允许为空
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .models import ChampionCombos

# 触发键合法集（小写）：字母 a-z + 主键盘数字 0-9
TRIGGER_KEYS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
# 序列按键合法集（小写）
SEQUENCE_KEYS = frozenset("qwerdf1234567asb")


def validate_combo(champ: ChampionCombos, trigger: str,
                   sequence: List, exclude: Optional[object] = None
                   ) -> Tuple[bool, List[str]]:
    """校验一条连招。返回 (是否通过, 错误列表)。

    exclude：编辑模式下传入被编辑的 Combo 对象，避免"trigger 未变"被误判为重复。
    """
    errors: List[str] = []
    t = (trigger or "").strip().lower()
    if not t:
        errors.append("触发键不能为空")
    elif t not in TRIGGER_KEYS:
        errors.append("触发键不合法")
    else:
        dup = champ.find_by_trigger(t)
        if dup is not None and dup is not exclude:
            errors.append("该触发键已被本英雄的另一个连招使用")
    steps = list(sequence or [])
    if not steps:
        errors.append("连招至少需要 1 个按键")
    for i, s in enumerate(steps, 1):
        k = (getattr(s, "key", "") or "").strip().lower()
        if k not in SEQUENCE_KEYS:
            errors.append(f"第 {i} 步按键不合法")
        try:
            d = int(getattr(s, "delay_after_ms", 0) or 0)
        except (TypeError, ValueError):
            errors.append(f"第 {i} 步延迟必须是 ≥ 0 的整数")
            d = 0
        if d < 0:
            errors.append(f"第 {i} 步延迟必须是 ≥ 0 的整数")
    return (not errors), errors
