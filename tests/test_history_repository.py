"""HistoryRepository unit tests"""
import os
import tempfile
import pytest

from app.player.match_repository import MatchRepository
from app.player.history_repository import HistoryRepository


def _seed(db_path):
    mr = MatchRepository(db_path)
    # completed match 1
    mr.create_match("m1", start_time=1000.0, game_mode="CLASSIC", map_name="SR")
    mr.finish_match("m1", end_time=1600.0, duration=600.0, result="Win")
    mr.save_match_players("m1", [
        {"puuid": "p-self", "riot_id": "self#tag", "team": "ORDER",
         "champion_name": "Ahri", "champion_id": 103, "position": "mid",
         "kills": 5, "deaths": 2, "assists": 3, "is_self": True},
        {"puuid": None, "riot_id": "foe#tag", "team": "CHAOS",
         "champion_name": "Darius", "champion_id": 1, "position": "top",
         "kills": 3, "deaths": 4, "assists": 2},
    ])
    # completed match 2
    mr.create_match("m2", start_time=2000.0, game_mode="CLASSIC", map_name="SR")
    mr.finish_match("m2", end_time=2600.0, duration=600.0, result="Lose")
    mr.save_match_players("m2", [
        {"puuid": "p-self", "riot_id": "self#tag", "team": "ORDER",
         "champion_name": "Ahri", "champion_id": 103, "position": "mid",
         "kills": 2, "deaths": 3, "assists": 5, "is_self": True},
        {"puuid": None, "riot_id": "foe2#tag", "team": "CHAOS",
         "champion_name": "Yasuo", "champion_id": 157, "position": "mid",
         "kills": 7, "deaths": 1, "assists": 4},
    ])
    # completed match 3 with different champion
    mr.create_match("m3", start_time=3000.0, game_mode="CLASSIC", map_name="SR")
    mr.finish_match("m3", end_time=3600.0, duration=600.0, result="Win")
    mr.save_match_players("m3", [
        {"puuid": "p-self", "riot_id": "self#tag", "team": "ORDER",
         "champion_name": "Garen", "champion_id": 86, "position": "top",
         "kills": 4, "deaths": 1, "assists": 2, "is_self": True},
    ])
    # incomplete match
    mr.create_match("m-incomplete", start_time=4000.0, game_mode="CLASSIC", map_name="SR")
    # no finish_match, no players
    return mr


class TestGetMatch:
    def test_existing(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            m = hr.get_match("m1")
            assert m is not None
            assert m["match"]["result"] == "Win"
            assert len(m["players"]) == 2

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            assert hr.get_match("nope") is None


class TestGetMatchPlayers:
    def test_returns_all(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            players = hr.get_match_players("m1")
            assert len(players) == 2


class TestGetRecentMatches:
    def test_default_excludes_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            recent = hr.get_recent_matches()
            ids = [m["local_match_id"] for m in recent]
            assert "m-incomplete" not in ids
            assert "m3" in ids

    def test_include_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            recent = hr.get_recent_matches(include_incomplete=True)
            ids = [m["local_match_id"] for m in recent]
            assert "m-incomplete" in ids

    def test_order_desc(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            recent = hr.get_recent_matches()
            assert recent[0]["start_time"] >= recent[-1]["start_time"]


class TestGetPlayerMatches:
    def test_puuid(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            ms = hr.get_player_matches(puuid="p-self")
            assert len(ms) == 3

    def test_riot_id_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            ms = hr.get_player_matches(riot_id="self#tag")
            assert len(ms) == 3

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            assert hr.get_player_matches(puuid="nobody") == []

    def test_excludes_incomplete_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            ms = hr.get_player_matches(puuid="p-self")
            assert "m-incomplete" not in [m["local_match_id"] for m in ms]


class TestChampionStats:
    def test_basic(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            stats = hr.get_champion_stats(puuid="p-self")
            by_champ = {s["champion_name"]: s for s in stats}
            assert by_champ["Ahri"]["games"] == 2
            assert by_champ["Ahri"]["wins"] == 1
            assert by_champ["Ahri"]["losses"] == 1
            assert by_champ["Ahri"]["kills"] == 7  # 5+2
            assert by_champ["Garen"]["games"] == 1
            assert by_champ["Garen"]["wins"] == 1


class TestRoleStats:
    def test_basic(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            hr = HistoryRepository(db)
            stats = hr.get_role_stats(puuid="p-self")
            by_pos = {s["position"]: s for s in stats}
            assert by_pos["mid"]["games"] == 2
            assert by_pos["top"]["games"] == 1


class TestReadonly:
    def test_no_writes(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            mr = MatchRepository(db)
            before_records = mr.count_matches()
            conn_before = __import__("sqlite3").connect(db)
            before_players = conn_before.execute("SELECT COUNT(*) FROM match_players").fetchone()[0]
            conn_before.close()

            hr = HistoryRepository(db)
            hr.get_match("m1")
            hr.get_recent_matches()
            hr.get_player_matches(puuid="p-self")
            hr.get_champion_stats(puuid="p-self")
            hr.get_role_stats(puuid="p-self")

            assert mr.count_matches() == before_records
            conn_after = __import__("sqlite3").connect(db)
            after_players = conn_after.execute("SELECT COUNT(*) FROM match_players").fetchone()[0]
            conn_after.close()
            assert after_players == before_players
