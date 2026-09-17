"""PlayerProfileService tests"""
import os
import tempfile
import sqlite3

from app.player.match_repository import MatchRepository
from app.player.history_repository import HistoryRepository
from app.player.profile_service import PlayerProfileService


def _seed(db):
    mr = MatchRepository(db)
    def mk(mid, st, result, champ, pos, k, d, a, team="ORDER"):
        mr.create_match(mid, st, "CLASSIC", "SR")
        mr.finish_match(mid, st+600, 600, result)
        mr.save_match_players(mid, [{
            "puuid": "p1", "riot_id": "me#tag", "team": team,
            "champion_name": champ, "position": pos,
            "kills": k, "deaths": d, "assists": a, "is_self": True,
        }])
    mk("m1", 1000, "Win", "Ahri", "mid", 5, 2, 3)
    mk("m2", 2000, "Lose", "Ahri", "mid", 2, 3, 5)
    mk("m3", 3000, "Win", "Garen", "top", 4, 1, 2)
    mk("m4", 4000, "Unknown", "Ahri", None, 1, 1, 1)
    # incomplete
    mr.create_match("m5", 5000, "CLASSIC", "SR")
    mr.save_match_players("m5", [{
        "puuid": "p1", "riot_id": "me#tag", "team": "ORDER",
        "champion_name": "Ahri", "position": "mid", "is_self": True,
    }])
    return mr


class TestProfileEmpty:
    def test_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            assert svc.get_player_profile(puuid="nobody") is None


class TestProfileSample:
    def test_counts(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1")
            assert p["sample"]["completed_games"] == 4
            assert p["sample"]["incomplete_games"] == 1
            assert p["sample"]["analyzed_games"] == 4


class TestProfileRecent:
    def test_order_desc(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1", limit_matches=2)
            assert len(p["recent_matches"]) == 2
            assert p["recent_matches"][0]["local_match_id"] == "m4"

    def test_incomplete_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1")
            ids = [m["local_match_id"] for m in p["recent_matches"]]
            assert "m5" not in ids


class TestChampionStats:
    def test_stats(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1")
            by_champ = {c["champion_name"]: c for c in p["champion_stats"]}
            assert by_champ["Ahri"]["games"] == 3  # m1 Win, m2 Lose, m4 Unknown
            assert by_champ["Ahri"]["wins"] == 1
            assert by_champ["Ahri"]["losses"] == 1  # Unknown not counted
            assert by_champ["Garen"]["games"] == 1
            assert by_champ["Garen"]["wins"] == 1


class TestRoleStats:
    def test_null_position(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1")
            positions = [r["position"] for r in p["role_stats"]]
            assert "mid" in positions
            assert "top" in positions
            assert "UNKNOWN" in positions


class TestUnknownResult:
    def test_unknown_not_loss(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1")
            by_champ = {c["champion_name"]: c for c in p["champion_stats"]}
            assert by_champ["Ahri"]["games"] == 3
            assert by_champ["Ahri"]["wins"] == 1
            assert by_champ["Ahri"]["losses"] == 1


class TestReadOnly:
    def test_no_db_changes(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            conn = sqlite3.connect(db)
            before_rec = conn.execute("SELECT COUNT(*) FROM match_records").fetchone()[0]
            before_mp = conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0]
            conn.close()

            svc = PlayerProfileService(HistoryRepository(db))
            svc.get_player_profile(puuid="p1")
            svc.get_player_profile(puuid="p1", limit_matches=2)

            conn = sqlite3.connect(db)
            assert conn.execute("SELECT COUNT(*) FROM match_records").fetchone()[0] == before_rec
            assert conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0] == before_mp
            conn.close()


class TestDeterminism:
    def test_same_result(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            r1 = svc.get_player_profile(puuid="p1")
            r2 = svc.get_player_profile(puuid="p1")
            assert r1 == r2


class TestLimitMatches:
    def test_limit2_only_stats_recent2(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)  # 4 completed: m4(Ahri Unknown), m3(Garen Win), m2(Ahri Lose), m1(Ahri Win)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1", limit_matches=2)
            # recent 2 = m4, m3
            assert len(p["recent_matches"]) == 2
            assert p["recent_matches"][0]["local_match_id"] == "m4"
            # champion_stats should only cover completed in pool: m4(Ahri Unknown) + m3(Garen Win)
            by_champ = {c["champion_name"]: c for c in p["champion_stats"]}
            assert by_champ["Ahri"]["games"] == 1
            assert by_champ["Ahri"]["wins"] == 0
            assert by_champ["Ahri"]["losses"] == 0  # Unknown
            assert by_champ["Garen"]["games"] == 1
            assert by_champ["Garen"]["wins"] == 1

    def test_limit_none_uses_all(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1", limit_matches=None)
            by_champ = {c["champion_name"]: c for c in p["champion_stats"]}
            assert by_champ["Ahri"]["games"] == 3
            assert by_champ["Garen"]["games"] == 1

    def test_limit0_empty(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1", limit_matches=0)
            assert p["recent_matches"] == []
            assert p["champion_stats"] == []
            assert p["sample"]["analyzed_games"] == 0
            assert p["sample"]["completed_games"] == 4  # total in DB

    def test_negative_limit_raises(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)
            svc = PlayerProfileService(HistoryRepository(db))
            try:
                svc.get_player_profile(puuid="p1", limit_matches=-1)
                assert False, "should raise"
            except ValueError:
                pass

    def test_include_incomplete_in_recent_not_stats(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            _seed(db)  # m5 is incomplete
            svc = PlayerProfileService(HistoryRepository(db))
            p = svc.get_player_profile(puuid="p1", limit_matches=5, include_incomplete=True)
            ids = [m["local_match_id"] for m in p["recent_matches"]]
            assert "m5" in ids  # incomplete in recent
            # stats only completed
            for c in p["champion_stats"]:
                assert c["games"] >= 1
            # sample: 4 completed, 1 incomplete
            assert p["sample"]["completed_games"] == 4
            assert p["sample"]["incomplete_games"] == 1
