"""Player Analysis unit tests"""
import pytest

pytestmark = pytest.mark.unit

import os
import tempfile
import pytest
from unittest.mock import MagicMock

from app.player.models import PlayerIdentity, GamePlayer, CurrentGamePlayers
from app.player.repository import PlayerRepository
from app.player.service import PlayerAnalysisService
from app.lol.snapshot import GameSnapshot, PlayerInfo


def _make_snapshot(players_data):
    snap = GameSnapshot()
    for p in players_data:
        pi = PlayerInfo(
            summoner_name=p["name"],
            champion_name=p["champ"],
            team=p["team"],
            is_current_player=p.get("is_self", False),
            position=p.get("pos", ""),
        )
        snap.players.append(pi)
        if pi.is_current_player:
            snap.current_player = pi
        elif pi.team == "ORDER":
            snap.ally_team.append(pi)
        else:
            snap.enemy_team.append(pi)
    return snap


class TestPlayerIdentity:
    def test_key_puuid(self):
        p = PlayerIdentity(puuid="abc", game_name="test", tag_line="NA1")
        assert p.key() == "abc"
        assert p.display_name() == "test#NA1"

    def test_key_fallback_no_puuid(self):
        p = PlayerIdentity(puuid="", summoner_id=123, platform="NA1")
        assert p.key() == "summoner:NA1:123"

    def test_display_no_tag(self):
        p = PlayerIdentity(game_name="justname")
        assert p.display_name() == "justname"


class TestRepository:
    def test_upsert_and_get(self):
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "test.db"))
            ident = PlayerIdentity(puuid="p1", game_name="Alice", tag_line="EUW",
                                   summoner_id=111, platform="EUW")
            repo.upsert(ident)
            got = repo.get("p1")
            assert got is not None
            assert got.game_name == "Alice"
            assert got.summoner_id == 111

    def test_upsert_update(self):
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "test.db"))
            repo.upsert(PlayerIdentity(puuid="p1", game_name="Old", tag_line="x"))
            repo.upsert(PlayerIdentity(puuid="p1", game_name="New", tag_line="y"))
            got = repo.get("p1")
            assert got.game_name == "New"
            assert len(repo.list_all()) == 1

    def test_get_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "test.db"))
            assert repo.get("nope") is None

    def test_empty_puuid_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "test.db"))
            repo.upsert(PlayerIdentity(puuid="", game_name="no_id"))
            assert len(repo.list_all()) == 0


class TestCurrentGamePlayers:
    def test_split_teams(self):
        cg = CurrentGamePlayers()
        cg.self_player = GamePlayer(summoner_name="self", team="ORDER")
        for i in range(4):
            cg.allies.append(GamePlayer(summoner_name=f"a{i}", team="ORDER"))
        for i in range(5):
            cg.enemies.append(GamePlayer(summoner_name=f"e{i}", team="CHAOS"))
        assert len(cg.all()) == 10
        assert cg.summary()["ally_count"] == 4
        assert cg.summary()["enemy_count"] == 5


class TestPlayerAnalysisService:
    def test_analyze_splits_teams(self):
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "p.db"))
            lcu = MagicMock()
            lcu.connected = True
            lcu.get_current_summoner.return_value = {"puuid": "self-puuid", "summonerId": 999}
            svc = PlayerAnalysisService(repo, lcu)

            players = [
                {"name": "You", "champ": "Ahri", "team": "ORDER", "is_self": True},
                {"name": "Mate1", "champ": "Lux", "team": "ORDER"},
                {"name": "Mate2", "champ": "Garen", "team": "ORDER"},
                {"name": "Mate3", "champ": "Jinx", "team": "ORDER"},
                {"name": "Mate4", "champ": "Thresh", "team": "ORDER"},
                {"name": "Foe1", "champ": "Darius", "team": "CHAOS"},
                {"name": "Foe2", "champ": "Yasuo", "team": "CHAOS"},
                {"name": "Foe3", "champ": "Zed", "team": "CHAOS"},
                {"name": "Foe4", "champ": "Caitlyn", "team": "CHAOS"},
                {"name": "Foe5", "champ": "Blitz", "team": "CHAOS"},
            ]
            snap = _make_snapshot(players)
            result = svc.analyze_game(snap)
            assert result is not None
            assert result.self_player.summoner_name == "You"
            assert len(result.allies) == 4
            assert len(result.enemies) == 5
            assert result.self_player.identity.puuid == "self-puuid"
            # self persisted
            assert repo.get("self-puuid") is not None

    def test_lcu_unavailable_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "p.db"))
            svc = PlayerAnalysisService(repo, lcu=None)
            players = [
                {"name": "You", "champ": "Ahri", "team": "ORDER", "is_self": True},
                {"name": "Foe1", "champ": "Darius", "team": "CHAOS"},
            ]
            result = svc.analyze_game(_make_snapshot(players))
            assert result is not None
            assert result.self_player.identity.puuid == ""

    def test_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "p.db"))
            svc = PlayerAnalysisService(repo, lcu=None)
            result = svc.analyze_game(GameSnapshot())
            assert result is None


class TestTeamLogic:
    def test_self_order_allies_order(self):
        """self=ORDER → ORDER teammates = allies, CHAOS = enemies"""
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "p.db"))
            svc = PlayerAnalysisService(repo, lcu=None)
            players = [
                {"name": "You", "champ": "Ahri", "team": "ORDER", "is_self": True},
                {"name": "Mate", "champ": "Lux", "team": "ORDER"},
                {"name": "Foe", "champ": "Darius", "team": "CHAOS"},
            ]
            result = svc.analyze_game(_make_snapshot(players))
            assert result.self_player.team == "ORDER"
            assert len(result.allies) == 1
            assert result.allies[0].summoner_name == "Mate"
            assert len(result.enemies) == 1
            assert result.enemies[0].summoner_name == "Foe"

    def test_self_chaos_allies_chaos(self):
        """self=CHAOS → CHAOS teammates = allies, ORDER = enemies"""
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "p.db"))
            svc = PlayerAnalysisService(repo, lcu=None)
            players = [
                {"name": "You", "champ": "Ahri", "team": "CHAOS", "is_self": True},
                {"name": "Mate", "champ": "Lux", "team": "CHAOS"},
                {"name": "Foe", "champ": "Darius", "team": "ORDER"},
            ]
            result = svc.analyze_game(_make_snapshot(players))
            assert result.self_player.team == "CHAOS"
            assert len(result.allies) == 1
            assert result.allies[0].summoner_name == "Mate"
            assert len(result.enemies) == 1
            assert result.enemies[0].summoner_name == "Foe"

    def test_self_unknown_no_crash(self):
        """self 无法识别时安全失败，不分类"""
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "p.db"))
            svc = PlayerAnalysisService(repo, lcu=None)
            players = [
                {"name": "P1", "champ": "Ahri", "team": "ORDER"},
                {"name": "P2", "champ": "Lux", "team": "CHAOS"},
            ]
            result = svc.analyze_game(_make_snapshot(players))
            assert result is not None
            assert result.self_player is None
            assert len(result.allies) == 0
            assert len(result.enemies) == 0


class TestChangeDetection:
    def test_no_reanalyze_when_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = PlayerRepository(os.path.join(td, "p.db"))
            svc = PlayerAnalysisService(repo, lcu=None)
            players = [
                {"name": "You", "champ": "Ahri", "team": "ORDER", "is_self": True},
                {"name": "Foe", "champ": "Darius", "team": "CHAOS"},
            ]
            snap = _make_snapshot(players)
            r1 = svc.analyze_game(snap)
            r2 = svc.analyze_game(snap)
            assert r1 is r2  # same object, no re-parse
