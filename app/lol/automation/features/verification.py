"""verification：执行后校验。参考 Syronss bot.py _verify_completed_action。"""
from .base import Feature, FeatureResult


class VerificationFeature(Feature):
    name = "verification"

    def tick(self, session, config) -> FeatureResult:
        if not self.enabled:
            return FeatureResult()
        # 实际校验由 ChampSelectController._verify_completed_action 执行；
        # 这里仅作为独立可观察的 feature 占位。
        return FeatureResult(detail={"verifier": "champ_select_controller._verify"})
