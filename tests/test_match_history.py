"""Match History unit tests"""
import os
import tempfile
import pytest

from app.player.match_repository import MatchRepository


class TestMatchRepository:
    def test_create_and_finish_match(self):
        with tempfile.TemporaryDirectory() as td:
            repo = MatchRepository(os.path.join(td, "p.db"))
            repo.create_match("mid-1", start_time=1000.0, game_mode="CLASSIC", map_name="Summoner's Rift")
            repo.finish_match("mid-1", end_time=1600.0, duration=600.0, result="Win")
            m = repo.get_match("mid-1")
            assert m is not None
            assert m["match"]["result"] == "Win"
            assert m["match"]["duration"] == 600.0

    def test_save_players(self):
        with tempfile.TemporaryDirectory() as td:
            repo = MatchRepository(os.path.join(td, "p.db"))
            repo.create_match("mid-1", 1000.0, "CLASSIC", "SR")
            players = [
                {"riot_id": "self#tag", "team": "ORDER", "champion_name": "Ahri",
                 "champion_id": 103, "kills": 5, "deaths": 2, "assists": 3,
                 "gold": 12000.0, "is_self": True},
                {"riot_id": "foe#tag", "team": "CHAOS", "champion_name": "Darius",
                 "champion_id": 1, "kills": 3, "deaths": 4, "assists": 2,
                 "gold": 10000.0},
            ]
            repo.save_match_players("mid-1", players)
            m = repo.get_match("mid-1")
            assert len(m["players"]) == 2
            self_data = [p for p in m["players"] if p["is_self"]][0]
            assert self_data["champion_name"] == "Ahri"

    def test_idempotent_players(self):
        with tempfile.TemporaryDirectory() as td:
            repo = MatchRepository(os.path.join(td, "p.db"))
            repo.create_match("mid-1", 1000.0, "CLASSIC", "SR")
            players = [{"riot_id": "self#tag", "team": "ORDER", "champion_name": "Ahri"}]
            repo.save_match_players("mid-1", players)
            repo.save_match_players("mid-1", players)  # duplicate
            m = repo.get_match("mid-1")
            assert len(m["players"]) == 1

    def test_get_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            repo = MatchRepository(os.path.join(td, "p.db"))
            assert repo.get_match("nope") is None

    def test_count(self):
        with tempfile.TemporaryDirectory() as td:
            repo = MatchRepository(os.path.join(td, "p.db"))
            assert repo.count_matches() == 0
            repo.create_match("m1", 1.0, "CLASSIC", "SR")
            repo.create_match("m2", 2.0, "CLASSIC", "SR")
            assert repo.count_matches() == 2


class TestSnapshotRiotId:
    def test_parse_player_extracts_riot_id(self):
        from app.lol.snapshot import SnapshotBuilder
        p_data = {
            "summonerName": "Test#TAG",
            "riotId": "Test#TAG",
            "riotIdGameName": "Test",
            "riotIdTagLine": "TAG",
            "championName": "Ahri",
            "championId": 103,
            "team": "ORDER",
        }
        p = SnapshotBuilder._parse_player(p_data, "Test#TAG")
        assert p.riot_id == "Test#TAG"
        assert p.riot_game_name == "Test"
        assert p.riot_tag_line == "TAG"
        assert p.is_current_player is True

    def test_parse_player_missing_riot_id(self):
        from app.lol.snapshot import SnapshotBuilder
        p_data = {
            "summonerName": "OldName",
            "championName": "Darius",
            "team": "CHAOS",
        }
        p = SnapshotBuilder._parse_player(p_data, "Other")
        assert p.riot_id == ""
        assert p.riot_game_name == ""


class TestMatchPlayerPuuid:
    def test_puuid_saved(self):
        with tempfile.TemporaryDirectory() as td:
            repo = MatchRepository(os.path.join(td, "p.db"))
            repo.create_match("m1", 1.0, "CLASSIC", "SR")
            repo.save_match_players("m1", [
                {"riot_id": "self#tag", "puuid": "self-puuid-123", "team": "ORDER", "champion_name": "Ahri"}
            ])
            m = repo.get_match("m1")
            assert m["players"][0]["puuid"] == "self-puuid-123"

    def test_puuid_null_for_others(self):
        with tempfile.TemporaryDirectory() as td:
            repo = MatchRepository(os.path.join(td, "p.db"))
            repo.create_match("m1", 1.0, "CLASSIC", "SR")
            repo.save_match_players("m1", [
                {"riot_id": "foe#tag", "puuid": None, "team": "CHAOS", "champion_name": "Darius"}
            ])
            m = repo.get_match("m1")
            assert m["players"][0]["puuid"] is None


class TestMatchFailureSafety:
    def test_db_failure_does_not_crash(self):
        """Repository 方法在 DB 路径无效时不抛异常到调用方"""
        with tempfile.TemporaryDirectory() as td:
            repo = MatchRepository(os.path.join(td, "p.db"))
            # 模拟一个只读路径导致写入失败
            repo.db_path = "/nonexistent-dir-xyz/abc.db"
            # 不应抛异常
            try:
                repo.create_match("m1", 1.0, "CLASSIC", "SR")
                repo.finish_match("m1", 2.0, 600.0, "Win")
                repo.save_match_players("m1", [{"riot_id": "x"}])
            except Exception as e:
                pytest.fail(f"Repository should swallow DB errors, got: {e}")

