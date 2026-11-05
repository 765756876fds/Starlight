"""当前对局标题时钟与卡片差分刷新回归。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from app.lol.ongoing.session import CurrentGamePlayer, CurrentGameSession
from app.ui.panels import current_game_panel as module

pytestmark = pytest.mark.ui


@pytest.fixture
def clock_panel(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(module, "_DEBUG_ONGOING", False)
    snapshot = SimpleNamespace(game_time=121.0)
    session = CurrentGameSession(
        game_id=1, phase="InProgress", started_at=0,
        my_team=[CurrentGamePlayer(
            puuid="clock-test", display_name="测试玩家", champion_id=157,
        )],
    )
    panel = module.CurrentGamePanel(
        snapshot_provider=lambda: snapshot,
        ongoing_service=SimpleNamespace(get_session=lambda: session),
    )
    panel._timer.stop()
    try:
        yield panel, snapshot, session
    finally:
        panel._timer.stop()
        panel.close()
        panel.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("start,end,expected", [
    (121, 122, "2:02"),
    (123, 124, "2:04"),
    (59, 60, "1:00"),
    (60, 61, "1:01"),
])
def test_title_tracks_each_new_second(clock_panel, start, end, expected):
    panel, snapshot, _ = clock_panel
    snapshot.game_time = start
    panel.refresh()
    snapshot.game_time = end
    panel.refresh()
    assert panel.lbl_title.text() == f"当前对局  时间 {expected}  玩家 1"


def test_seconds_do_not_force_player_card_redraw(clock_panel, monkeypatch):
    panel, snapshot, _ = clock_panel
    fill = Mock(wraps=panel._fill_player_card)
    monkeypatch.setattr(panel, "_fill_player_card", fill)
    panel.refresh()
    card = panel.ally_cards[0]
    for seconds in (122, 123, 124):
        snapshot.game_time = seconds
        panel.refresh()
        assert panel.lbl_title.text() == f"当前对局  时间 2:{seconds - 120:02d}  玩家 1"
        assert panel.ally_cards[0] is card
    assert fill.call_count == 1


def test_leaving_game_resets_title_and_next_game_updates(clock_panel):
    panel, snapshot, session = clock_panel
    panel.refresh()
    player = session.my_team.pop()
    panel.refresh()
    assert panel.lbl_title.text() == "未在对局中 / 选人中"
    assert panel.ally_cards[0].isHidden()

    session.game_id = 2
    session.my_team.append(player)
    snapshot.game_time = 1
    panel.refresh()
    assert panel.lbl_title.text() == "当前对局  时间 0:01  玩家 1"
    snapshot.game_time = 2
    panel.refresh()
    assert panel.lbl_title.text() == "当前对局  时间 0:02  玩家 1"
