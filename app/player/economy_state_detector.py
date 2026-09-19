"""A5 EconomyStateDetector - 确定性经济状态边沿检测。

只输出“状态跨越”这一个事实，不预测胜率、不给建议、不用 wall-clock。
状态阈值与 A4 economy() 一致：|diff| < 2000 = NEAR。
初始化静默：第一帧只记录状态、不播报；真正跨边界才给一句短文本。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

ECONOMY_EVEN = 2000


class EconomyState(str, Enum):
    BEHIND = "BEHIND"
    NEAR = "NEAR"
    AHEAD = "AHEAD"


def classify(diff: int) -> EconomyState:
    if diff > ECONOMY_EVEN:
        return EconomyState.AHEAD
    if diff < -ECONOMY_EVEN:
        return EconomyState.BEHIND
    return EconomyState.NEAR


class EconomyStateDetector:
    def __init__(self, threshold: int = ECONOMY_EVEN):
        self.threshold = threshold
        self._state: Optional[EconomyState] = None  # None = 尚未初始化

    def reset(self) -> None:
        """GAME_STARTED / GAME_ENDED 后调用：下一局第一帧静默初始化。"""
        self._state = None

    def update(self, diff: int) -> Optional[str]:
        """输入当前经济差（our-enemy）。跨边界返回一句短提醒，否则 None。

        第一帧（_state is None）只初始化、不播报。
        """
        st = classify(diff)
        if self._state is None:
            self._state = st
            return None
        if st == self._state:
            return None
        # 真正跨越
        self._state = st
        if st == EconomyState.AHEAD:
            return f"现在领先大约 {abs(int(diff))} 经济。"
        if st == EconomyState.BEHIND:
            return f"现在落后大约 {abs(int(diff))} 经济。"
        return "经济回到接近。"
