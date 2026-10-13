"""修仙称号计算：胜率 + KDA 双维度判定，严格按表、无偏档。

输入：近期聚合统计（近 20 场过滤）+ 当前段位（可选）
输出：TitleResult(text=称号, color=档位色)
  - 有段位：12 档（X境·称号，从上到下第一命中）
  - 无段位：5 档（纯称号，无"境"前缀）
  - 无有效场（调用方已保证 AVAILABLE 才进来）→ 调用方不调本函数
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 段位中文映射（与 UI 一致，title 模块自带避免循环依赖）
TIER_ZH = {
    "IRON": "黑铁", "BRONZE": "青铜", "SILVER": "白银", "GOLD": "黄金",
    "PLATINUM": "铂金", "EMERALD": "翡翠", "DIAMOND": "钻石", "MASTER": "大师",
    "GRANDMASTER": "宗师", "CHALLENGER": "王者",
}

# 档 4（跌落）：段位降 1 大段映射（IRON 降无可降，保持原段位）
_TIER_DOWN = {
    "CHALLENGER": "GRANDMASTER", "GRANDMASTER": "MASTER", "MASTER": "DIAMOND",
    "DIAMOND": "EMERALD", "EMERALD": "PLATINUM", "PLATINUM": "GOLD",
    "GOLD": "SILVER", "SILVER": "BRONZE", "BRONZE": "IRON", "IRON": "IRON",
}

# 档位颜色（dark 主题下可读；"黑"用近黑深灰，纯黑在深背景不可见）
COLORS = {
    1: "#ffd700",   # 金（请神）
    2: "#a855f7",   # 紫金（天人合一）
    3: "#9090a8",   # 灰（道心破碎）
    4: "#3f3f46",   # 黑·近黑（跌落）
    5: "#4a9eff",   # 蓝（大圆满）
    6: "#4ade80",   # 绿（圆满）
    7: "#facc15",   # 黄（半步）
    8: "#86efac",   # 浅绿（福缘）
    9: "#c084fc",   # 浅紫（孤高）
    10: "#b0b8c4",  # 浅灰（中庸）
    11: "#71717a",  # 深灰（式微）
    12: "#dc2626",  # 暗红（衰败）
}

# 有段位档位后缀（档 4 跌落 / 档 7 半步 特殊拼接；档 3 道心破碎常规拼接）
_TITLE_SUFFIX = {
    1: "请神", 2: "天人合一", 3: "道心破碎", 5: "大圆满", 6: "圆满",
    8: "福缘", 9: "孤高", 10: "中庸", 11: "式微", 12: "衰败",
}

# 无段位 5 档：纯称号（无"境"前缀），颜色复用 COLORS
_NO_TIER_NAMES = {1: "造化", 2: "精进", 3: "守常", 4: "困顿", 5: "沉沦"}
_NO_TIER_COLOR_KEY = {1: 1, 2: 5, 3: 6, 4: 3, 5: 12}  # 金/蓝/绿/灰/暗红

# KDA 等级阈值（C 位 / 辅助）——半开区间：[high) [mid_high) [mid) [mid_low) [low)
_KDA_HIGH = (4.0, 2.5)
_KDA_MID_HIGH = (3.0, 2.0)
_KDA_MID = (2.0, 1.5)
_KDA_MID_LOW = (1.5, 1.2)

# 等级序（用于档位判定）
_LEVEL_ORDER = ["high", "mid_high", "mid", "mid_low", "low"]


@dataclass
class TitleResult:
    text: str
    color: str


def _kda_level(kda: float, is_support: bool) -> str:
    """KDA → 等级（C 位/辅助两套阈值）。"""
    if kda >= (_KDA_HIGH[1] if is_support else _KDA_HIGH[0]):
        return "high"
    if kda >= (_KDA_MID_HIGH[1] if is_support else _KDA_MID_HIGH[0]):
        return "mid_high"
    if kda >= (_KDA_MID[1] if is_support else _KDA_MID[0]):
        return "mid"
    if kda >= (_KDA_MID_LOW[1] if is_support else _KDA_MID_LOW[0]):
        return "mid_low"
    return "low"


def _at_least(lvl: str, floor: str) -> bool:
    """lvl 是否达到 floor（含）。"""
    return _LEVEL_ORDER.index(lvl) <= _LEVEL_ORDER.index(floor)


def _make(level: int, tier: str) -> TitleResult:
    """有段位：X境·称号（档 4 降 1 段、档 7 半步 特例）。"""
    tier_zh = TIER_ZH.get(tier.upper(), tier)
    if level == 7:
        text = f"半步{tier_zh}境"
    elif level == 4:
        down = _TIER_DOWN.get(tier.upper(), tier)
        down_zh = TIER_ZH.get(down, down)
        text = f"{down_zh}境·跌落"
    else:
        text = f"{tier_zh}境·{_TITLE_SUFFIX[level]}"
    return TitleResult(text=text, color=COLORS[level])


def _make_no_tier(level: int) -> TitleResult:
    """无段位：纯称号（无"境"前缀）。"""
    return TitleResult(text=_NO_TIER_NAMES[level], color=COLORS[_NO_TIER_COLOR_KEY[level]])


def compute_title(
    win_rate: float,
    average_kda: float,
    streak: int,
    utility_ratio: float,
    tier: Optional[str],
    games_10: int = 0,
    win_rate_10: float = 0.0,
    average_kda_10: float = 0.0,
    streak_10: int = 0,
) -> Optional[TitleResult]:
    """12 档（有段位）+ 5 档（无段位），从上到下第一命中。

    - 有段位 A 套：档 1/2（请神/天人合一）用最近 10 场，<5 场不评；其余用最近 20 场
    - 无段位 B 套：5 档全用 20 场，不看连胜
    - 辅助判定：近 20 场 UTILITY 占比 > 50% → 辅助 KDA 阈值（两窗口统一）
    - A 套 12 档覆盖全部 win_rate×KDA 组合（几乎无落空，除 10 场档 <5 场回落）
    """
    is_support = (utility_ratio or 0.0) > 0.5
    wr = win_rate
    lvl = _kda_level(average_kda, is_support)

    # ---- B 套：无段位 5 档（20 场窗口） ----
    if not tier:
        if wr >= 0.60 and lvl == "high":
            return _make_no_tier(1)   # 造化（金）
        if wr >= 0.55:
            return _make_no_tier(2)   # 精进（蓝，非造化）
        if 0.45 <= wr < 0.55:
            return _make_no_tier(3)   # 守常（绿）
        if 0.35 <= wr < 0.45:
            return _make_no_tier(4)   # 困顿（灰）
        return _make_no_tier(5)       # 沉沦（暗红，wr<0.35）

    # ---- A 套：有段位 12 档 ----
    lvl10 = _kda_level(average_kda_10, is_support)
    # 1 X境·请神（金）：10 场胜率≥75% + 连胜10≥5 + KDA10 高（<5 场不评）
    if games_10 >= 5 and win_rate_10 >= 0.75 and streak_10 >= 5 and lvl10 == "high":
        return _make(1, tier)
    # 2 X境·天人合一（紫金）：10 场胜率≥65% + 连胜10≥3 + KDA10 中高以上
    if games_10 >= 5 and win_rate_10 >= 0.65 and streak_10 >= 3 \
            and _at_least(lvl10, "mid_high"):
        return _make(2, tier)
    # 3 X境·道心破碎（灰）：20 场胜率<45% + KDA 中高以上（尽力局局长）
    if wr < 0.45 and _at_least(lvl, "mid_high"):
        return _make(3, tier)
    # 4 (降1段)X境·跌落（黑）：20 场胜率<40% + 连败 + KDA 低
    if wr < 0.40 and streak < 0 and lvl == "low":
        return _make(4, tier)
    # 5 X境·大圆满（蓝）：20 场胜率≥55% + KDA 中以上
    if wr >= 0.55 and _at_least(lvl, "mid"):
        return _make(5, tier)
    # 6 X境·圆满（绿）：20 场胜率 45~55% + KDA 中
    if 0.45 <= wr < 0.55 and lvl == "mid":
        return _make(6, tier)
    # 7 半步X境（黄）：20 场胜率 35~45% + KDA 中低
    if 0.35 <= wr < 0.45 and lvl == "mid_low":
        return _make(7, tier)
    # 8 X境·福缘（浅绿）：20 场胜率≥55% + KDA 中低/低
    if wr >= 0.55 and lvl in ("mid_low", "low"):
        return _make(8, tier)
    # 9 X境·孤高（浅紫）：20 场胜率 45~55% + KDA 高/中高
    if 0.45 <= wr < 0.55 and lvl in ("high", "mid_high"):
        return _make(9, tier)
    # 10 X境·中庸（浅灰）：20 场胜率 45~55% + KDA 中低/低
    if 0.45 <= wr < 0.55 and lvl in ("mid_low", "low"):
        return _make(10, tier)
    # 11 X境·式微（深灰）：20 场胜率 35~45% + KDA 中/低
    if 0.35 <= wr < 0.45 and lvl in ("mid", "low"):
        return _make(11, tier)
    # 12 X境·衰败（暗红）：20 场胜率<35% + KDA 中/中低/低（其余组合已被档 3 覆盖）
    return _make(12, tier)
