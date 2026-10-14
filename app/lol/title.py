"""修仙称号 8 档判定：胜率 + KDA 双维度，C 位/辅助两套阈值，全覆盖无落空。

输入：50 场过滤后聚合统计（recent_win_rate / recent_average_kda / streak /
      utility_ratio，均来自 intel 链路，与显示行同源）。
输出：TitleResult(text=称号, color=档位色)
  - 有段位：X境·称号（8 档，从上到下第一命中）
  - 无段位：纯称号（8 档同名，无"X境"前缀）
  - 全覆盖：档 8 兜底，任意组合必命中
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

# 档位颜色（dark 主题下可读）
COLORS = {
    1: "#FFD700",   # 金（请神）
    2: "#FF4757",   # 血红（天人合一）—— 提亮
    3: "#5B9BFF",   # 亮蓝（大圆满）—— 提亮
    4: "#B388FF",   # 紫（孤高）—— 提亮
    5: "#52D65B",   # 绿（稳健）—— 提亮
    6: "#A0A0A0",   # 灰（颓势）—— 提亮
    7: "#8A8A8A",   # 深灰（力竭）—— 大幅提亮
    8: "#C84B4B",   # 暗红（崩盘）—— 提亮
}

# 档名（有段位拼"X境·"前缀；无段位直接用）
_NAMES = {
    1: "请神", 2: "天人合一", 3: "大圆满", 4: "孤高",
    5: "稳健", 6: "颓势", 7: "力竭", 8: "崩盘",
}

# KDA 阈值（C 位 / 辅助）：1-7 档各一对；档 8 用档 6 阈值判断"低 KDA"
_KDA = {
    1: (4.5, 3.5),
    2: (3.8, 3.0),
    3: (3.0, 2.5),
    4: (3.5, 2.8),
    5: (2.2, 1.8),
    6: (1.5, 1.2),
    7: (1.0, 0.8),
}


@dataclass
class TitleResult:
    text: str
    color: str


def _kda_ge(kda: float, level: int, is_support: bool) -> bool:
    """kda 是否达到某档阈值（按位置取 C 位/辅助值）。"""
    th = _KDA[level][1] if is_support else _KDA[level][0]
    return kda >= th


def _make(level: int, tier: Optional[str]) -> TitleResult:
    """有段位：X境·称号；无段位：纯称号。"""
    name = _NAMES[level]
    if tier:
        tier_zh = TIER_ZH.get(tier.upper(), tier)
        text = f"{tier_zh}境·{name}"
    else:
        text = name
    return TitleResult(text=text, color=COLORS[level])


def compute_title(
    win_rate: float,
    average_kda: float,
    streak: int,
    utility_ratio: float,
    tier: Optional[str],
) -> TitleResult:
    """8 档判定（从上到下第一命中，档 8 兜底全覆盖）。

    - 判定窗口：50 场过滤后的实际场次（与显示行同源）
    - 辅助判定：utility_ratio > 0.5 → 辅助 KDA 阈值（沿用现有）
    - 档 8 双路径：兜底（其余组合）或 5+连败 + 低 KDA
    """
    is_support = (utility_ratio or 0.0) > 0.5
    wr = win_rate
    kda = average_kda

    # 1 请神（金）：胜率≥65% + KDA 高；或 5+连胜 + KDA 中高（个人近期爆表）
    if wr >= 0.65 and _kda_ge(kda, 1, is_support):
        return _make(1, tier)
    if streak >= 5 and _kda_ge(kda, 4, is_support):
        return _make(1, tier)
    # 2 天人合一（血红）：胜率≥58% + KDA 中高
    if wr >= 0.58 and _kda_ge(kda, 2, is_support):
        return _make(2, tier)
    # 3 大圆满（亮蓝）：胜率≥52% + KDA 中
    if wr >= 0.52 and _kda_ge(kda, 3, is_support):
        return _make(3, tier)
    # 4 孤高（紫）：KDA 高 + 胜率<45%（个人碾压但团队拖）
    if _kda_ge(kda, 4, is_support) and wr < 0.45:
        return _make(4, tier)
    # 5 稳健（绿）：胜率≥45% + KDA 中低
    if wr >= 0.45 and _kda_ge(kda, 5, is_support):
        return _make(5, tier)
    # 6 颓势（灰）：胜率≥38% + KDA 低
    if wr >= 0.38 and _kda_ge(kda, 6, is_support):
        return _make(6, tier)
    # 7 力竭（深灰）：胜率≥32% + KDA 很低
    if wr >= 0.32 and _kda_ge(kda, 7, is_support):
        return _make(7, tier)
    # 8 崩盘（暗褐红）：其余；或 5+连败 + KDA 低（<档 6 阈值）
    if streak <= -5 and not _kda_ge(kda, 6, is_support):
        return _make(8, tier)
    return _make(8, tier)
