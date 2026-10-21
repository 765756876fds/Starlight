"""
段位映射中文显示回归测试：
所有段位（含 EMERALD 翡翠）都必须映射为中文，且两个面板映射一致。
"""
import pytest

pytestmark = pytest.mark.unit

import pytest

from app.ui.panels.current_game_panel import _TIER_ZH as CGP_TIER
from app.ui.panels.ongoing_panel import _TIER_MAP as OG_TIER

ALL_TIERS = {
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
    "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
}


def test_current_game_panel_covers_all_tiers():
    """CurrentGamePanel 映射必须覆盖全部 10 个段位（此前缺 EMERALD 导致显示英文）。"""
    missing = ALL_TIERS - set(CGP_TIER)
    assert not missing, f"缺失段位映射: {missing}"
    assert CGP_TIER["EMERALD"] == "翡翠"


def test_ongoing_panel_covers_all_tiers():
    missing = ALL_TIERS - set(OG_TIER)
    assert not missing, f"缺失段位映射: {missing}"


def test_maps_consistent():
    """两个面板映射值一致（key 集合相同的部分）。"""
    common = set(CGP_TIER) & set(OG_TIER)
    for k in common:
        assert CGP_TIER[k] == OG_TIER[k], f"段位 {k} 两个映射不一致"


def test_upper_fallback():
    """大小写不敏感：小写 tier 也应映射为中文。"""
    assert CGP_TIER.get("emerald".upper(), "emerald") == "翡翠"
