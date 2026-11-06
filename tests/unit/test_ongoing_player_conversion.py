"""Pure player conversion and compatibility with ongoing service consumers."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.lol.ongoing.player_conversion import to_current_player
from app.lol.ongoing.service import OngoingGameService
from app.lol.ongoing.session import CurrentGamePlayer


@pytest.mark.parametrize('field', ['riotIdGameName', 'riotId', 'name', 'summonerName'])
def test_display_name_fallback_order(field):
    names = ['riotIdGameName', 'riotId', 'name', 'summonerName']
    cell = {key: key for key in names[names.index(field):]}
    assert to_current_player(cell, 'myTeam', None).display_name == field


@pytest.mark.parametrize('team', ['myTeam', 'theirTeam'])
@pytest.mark.parametrize('me', ['self-id', 'other-id', None])
def test_identity_realtime_and_history_remain_separate(team, me):
    cell = dict(puuid='self-id', summonerId=31, cellId=2, championId=54,
                championPickIntent=1, riotIdGameName='测试玩家', assignedPosition='TOP',
                live_kills=3, live_deaths=2, live_assists=8, live_position='TOP',
                live_level=9, live_merged=True, history={'untrusted': True})
    original = deepcopy(cell)
    player = to_current_player(cell, team, me)
    assert player == CurrentGamePlayer(
        puuid='self-id', summoner_id=31, cell_id=2, champion_id=54,
        champion_pick_intent=1, display_name='测试玩家', assigned_position='TOP',
        team=team, is_self=me == 'self-id', live_kills=3, live_deaths=2,
        live_assists=8, live_position='TOP', live_level=9, realtime_available=True,
    )
    assert player.history is None and cell == original
    assert asdict(OngoingGameService._to_player(cell, team, me)) == asdict(player)


@pytest.mark.parametrize('value', [None, '', 0])
def test_missing_fields_keep_existing_defaults(value):
    keys = ('puuid', 'summonerId', 'cellId', 'championId', 'championPickIntent',
            'riotIdGameName', 'assignedPosition', 'live_kills', 'live_deaths',
            'live_assists', 'live_position', 'live_level', 'live_merged')
    assert to_current_player(dict.fromkeys(keys, value), 'myTeam', None) == CurrentGamePlayer()


def test_realtime_values_do_not_imply_live_data_is_available():
    player = to_current_player(dict(live_kills=2, live_level=5), 'theirTeam', None)
    assert player.live_kills == 2 and player.live_level == 5
    assert player.realtime_available is False


def test_repeated_conversion_returns_independent_players():
    cell = dict(puuid='p', riotIdGameName='name')
    a = to_current_player(cell, 'myTeam', 'p')
    b = to_current_player(cell, 'myTeam', 'p')
    assert a is not b and a == b
    a.display_name = 'changed'
    assert b.display_name == 'name' and cell['riotIdGameName'] == 'name'


def test_session_consumer_still_calls_overridable_static_entry(monkeypatch):
    calls = []

    class Service(OngoingGameService):
        @staticmethod
        def _to_player(cell, team, me):
            calls.append((cell['puuid'], team, me))
            return to_current_player(cell, team, me)

    svc = Service.__new__(Service)
    monkeypatch.setattr('app.lol.ongoing.service.time', SimpleNamespace(time=lambda: 123.5))
    raw = dict(gameId=42, myTeam=[{'puuid': 'a'}], theirTeam=[{'puuid': 'b'}])
    original = deepcopy(raw)
    session = svc._to_current_session(raw, 'InProgress', 'a')
    assert calls == [('a', 'myTeam', 'a'), ('b', 'theirTeam', 'a')]
    assert session.game_id == 42 and session.phase == 'InProgress'
    assert session.started_at == session.updated_at == 123.5
    assert session.my_team[0].is_self and not session.their_team[0].is_self
    assert raw == original
