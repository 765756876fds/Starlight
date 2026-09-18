"""auto_ban / auto_pick：ChampSelect 阶段 ban/pick。
参考 LeagueAkari ban-pick-controller.ts + Syronss bot.py handle_champ_select。
"""
from __future__ import annotations

from .base import Feature, FeatureResult
from ..champ_select_controller import ChampSelectController


class _ChampSelectFeature(Feature):
    """共享 ChampSelectController；auto_ban / auto_pick 各自开关。"""
    def __init__(self, controller: ChampSelectController, enabled: bool = False):
        super().__init__(enabled=enabled)
        self._ctl = controller

    def tick(self, session, config) -> FeatureResult:
        if not self.enabled:
            return FeatureResult()
        phase = getattr(session, "phase", None) if session is not None else None
        if phase != "ChampSelect":
            return FeatureResult()
        self._ctl.poll(config)
        return FeatureResult(detail={"phase": phase})


class AutoBanFeature(_ChampSelectFeature):
    name = "auto_ban"


class AutoPickFeature(_ChampSelectFeature):
    name = "auto_pick"
