"""CurrentGamePanel.is_mastery 数据源测试（LCU mastery_top3 权威判定）。

背景：current_game_panel.py L471-478 已从 loader top_champions 切到
intel_entry.current_champion_is_mastery_top3（方案 A）。
断言：intel_entry 就绪 → 三态透传 True/False/None；
      无 intel 服务 → None（UI 显示"未知"）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
pytestmark = pytest.mark.ui


from app.ui.panels.current_game_panel import CurrentGamePanel
from app.lol.ongoing.intelligence_service import PlayerIntelligenceCacheEntry


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _make_player():
    return SimpleNamespace(
        history=None, champion_id=1, display_name="T", is_self=False,
        puuid="p1", live_level=0, live_position=None, assigned_position=None,
        live_kills=0, live_deaths=0, live_assists=0,
    )


def _make_intel(entry):
    m = MagicMock()
    m.get_cached.return_value = entry  # 非 None → _ensure_intel L278 提前 return，不起线程
    m.should_throttle.return_value = False
    return m


def _run(panel_intel):
    """构造 panel → 注入 intel → 调 _fill_player_card → 取 update_data 的 is_mastery 实参。"""
    panel = CurrentGamePanel()
    panel._intelligence = panel_intel
    card = MagicMock()
    panel._fill_player_card(card, _make_player())
    return card.update_data.call_args.kwargs["is_mastery"]


def test_mastery_true(qapp):
    entry = PlayerIntelligenceCacheEntry(puuid="p1", current_champion_is_mastery_top3=True)
    assert _run(_make_intel(entry)) is True


def test_mastery_false(qapp):
    entry = PlayerIntelligenceCacheEntry(puuid="p1", current_champion_is_mastery_top3=False)
    assert _run(_make_intel(entry)) is False


def test_mastery_unknown(qapp):
    entry = PlayerIntelligenceCacheEntry(puuid="p1", current_champion_is_mastery_top3=None)
    assert _run(_make_intel(entry)) is None


def test_no_intel_service(qapp):
    # _intelligence=None：L353 短路，不做判定，is_mastery 保持初始值 None（UI"未知"）
    assert _run(None) is None
