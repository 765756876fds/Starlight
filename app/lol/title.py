"""修仙称号计算：胜率 + KDA 双维度判定，严格按表、无偏档。

输入：近期聚合统计（近 20 场过滤 PVE）+ 当前段位
输出：TitleResult(text=称号, color=档位色) 或 None（无档可套，保留原段位显示）
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

# 档 7（跌落）：段位降 1 大段映射（IRON 降无可降，保持原段位）
_TIER_DOWN = {
    "CHALLENGER": "GRANDMASTER", "GRANDMASTER": "MASTER", "MASTER": "DIAMOND",
    "DIAMOND": "EMERALD", "EMERALD": "PLATINUM", "PLATINUM": "GOLD",
    "GOLD": "SILVER", "SILVER": "BRONZE", "BRONZE": "IRON", "IRON": "IRON",
}

# 档位颜色（dark 主题下可读；"黑"用近黑深灰，纯黑在深背景不可见）
COLORS = {
    1: "#ffd700",   # 金
    2: "#a855f7",   # 紫金
    3: "#4a9eff",   # 蓝
    4: "#4ade80",   # 绿
    5: "#facc15",   # 黄
    6: "#9090a8",   # 灰
    7: "#3f3f46",   # 黑（近黑）
}

# 档 1-4、6 的后缀（档 5 "半步X境"、档 7 "降段境·跌落" 特殊拼接）
_TITLE_SUFFIX = {
    1: "请神", 2: "天人合一", 3: "大圆满", 4: "圆满", 6: "道心破碎",
}

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
    tier_zh = TIER_ZH.get(tier.upper(), tier)
    if level == 5:
        text = f"半步{tier_zh}境"
    elif level == 7:
        down = _TIER_DOWN.get(tier.upper(), tier)
        down_zh = TIER_ZH.get(down, down)
        text = f"{down_zh}境·跌落"
    else:
        text = f"{tier_zh}境·{_TITLE_SUFFIX[level]}"
    return TitleResult(text=text, color=COLORS[level])


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
    """7 档严格按表判定（从上到下第一命中）。

    - 窗口：档 1/2（请神/天人合一）用最近 10 场，且 <5 场不评；其余用最近 20 场
    - 辅助判定：近 20 场 UTILITY 占比 > 50% → 辅助 KDA 阈值（两窗口统一）
    - 落空区间返回 None（不硬套档位，UI 保留原段位显示）
    - 档 7：段位降 1 大段（翡翠→铂金）；档 1 不升档
    """
    if not tier:
        return None

    is_support = (utility_ratio or 0.0) > 0.5
    wr = win_rate
    lvl = _kda_level(average_kda, is_support)

    # 1 X境·请神（金）：10 场胜率≥75% + 连胜≥5 + KDA 高（<5 场不评）
    if games_10 >= 5 and win_rate_10 >= 0.75 and streak_10 >= 5 \
            and _kda_level(average_kda_10, is_support) == "high":
        return _make(1, tier)
    # 2 X境·天人合一（紫金）：10 场胜率≥65% + 连胜≥3 + KDA 中高以上（<5 场不评）
    if games_10 >= 5 and win_rate_10 >= 0.65 and streak_10 >= 3 \
            and _at_least(_kda_level(average_kda_10, is_support), "mid_high"):
        return _make(2, tier)
    # 3 X境·大圆满（蓝）：20 场胜率≥55% + KDA 中以上
    if wr >= 0.55 and _at_least(lvl, "mid"):
        return _make(3, tier)
    # 4 X境·圆满（绿）：20 场胜率 45~55% + KDA 中
    if 0.45 <= wr < 0.55 and lvl == "mid":
        return _make(4, tier)
    # 5 半步X境（黄）：20 场胜率 35~45% + KDA 中低
    if 0.35 <= wr < 0.45 and lvl == "mid_low":
        return _make(5, tier)
    # 6 X境·道心破碎（灰）：20 场胜率<45% + KDA 中高以上（尽力局局长）
    if wr < 0.45 and _at_least(lvl, "mid_high"):
        return _make(6, tier)
    # 7 (降1档)X境·跌落（黑）：20 场胜率<40% + 连败 + KDA 低
    if wr < 0.40 and streak < 0 and lvl == "low":
        return _make(7, tier)

    return None
