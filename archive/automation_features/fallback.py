"""fallback：候选推进。参考 Syronss bot.py _get_available_champion + _rejected_candidates。"""
from .base import Feature, FeatureResult
from ..priority import pick_available


class FallbackFeature(Feature):
    name = "fallback"

    def tick(self, session, config) -> FeatureResult:
        if not self.enabled:
            return FeatureResult()
        return FeatureResult(detail={"selector": pick_available.__name__})
