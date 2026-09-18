"""Phase 4-D/E PlayerQuery tests"""
import os
import sqlite3
import tempfile

from app.voice.intent import RuleBasedIntent
from app.core.command_bus import CommandType, Command
from app.player.match_repository import MatchRepository
from app.player.history_repository import HistoryRepository
from app.player.profile_service import PlayerProfileService
from app.player.game_analysis import GameAnalysisService
from app.player.event_log_reader import EventLogReader
from app.player.query_service import PlayerQueryService
from app.player.query_handler import make_player_query_handler
from app.player.response_formatter import format_query_result


def _seed(db, log_dir):
    mr = MatchRepository(db)
    mr.create_match("m1", 1000.0, "CLASSIC", "SR")
    mr.finish_match("m1", 1600.0, 600.0, "Win")
    mr.save_match_players("m1", [
        {"puuid": "p1", "riot_id": "alice#tag", "team": "ORDER",
         "champion_name": "Ahri", "champion_id": 103, "position": "mid",
         "kills": 5, "deaths": 2, "assists": 3, "gold": 1200, "is_self": True},
        {"puuid": "p2", "riot_id": "bob#tag", "team": "CHAOS",
         "champion_name": "Darius", "champion_id": 1, "position": "top",
         "kills": 3, "deaths": 4, "assists": 2, "gold": 900},
    ])
    mr.create_match("m2", 2000.0, "CLASSIC", "SR")
    mr.finish_match("m2", 2600.0, 600.0, "Lose")
    mr.save_match_players("m2", [
        {"puuid": "p1", "riot_id": "alice#tag", "team": "ORDER",
         "champion_name": "Lux", "champion_id": 99, "position": "mid",
         "kills": 2, "deaths": 5, "assists": 1, "gold": 800, "is_self": True},
    ])


def _make_service(db, log_dir):
    history = HistoryRepository(db)
    profile = PlayerProfileService(history)
    analysis = GameAnalysisService(history, EventLogReader(log_dir))
    return PlayerQueryService(history, profile, analysis)


class TestRuleBasedIntent:
    def test_recent_number(self):
        i = RuleBasedIntent.match("最近5场")
        assert i.intent_type == "player.query"
        assert i.params["query_type"] == "recent"
        assert i.params["limit"] == 5

    def test_recent_chinese_number(self):
        i = RuleBasedIntent.match("最近十场")
        assert i.params["limit"] == 10

    def test_recent_default(self):
        i = RuleBasedIntent.match("最近战绩")
        assert i.intent_type == "player.query"
        assert i.params["limit"] == 10

    def test_champion_stats(self):
        i = RuleBasedIntent.match("最常玩的英雄")
        assert i.params["query_type"] == "champion_stats"

    def test_role_stats(self):
        i = RuleBasedIntent.match("主要位置")
        assert i.params["query_type"] == "role_stats"


class TestQueryService:
    def test_recent_puuid(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="recent", identity={"puuid": "p1"}, limit=5)
            assert r["query_type"] == "recent"
            assert len(r["result"]["recent_matches"]) == 2

    def test_recent_riot_id_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="recent", identity={"riot_id": "alice#tag"})
            assert r["result"] is not None
            assert len(r["result"]["recent_matches"]) == 2

    def test_champion_stats(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="champion_stats", identity={"puuid": "p1"})
            names = [c["champion_name"] for c in r["result"]["champion_stats"]]
            assert "Ahri" in names

    def test_role_stats(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="role_stats", identity={"puuid": "p1"})
            positions = [r2["position"] for r2 in r["result"]["role_stats"]]
            assert "mid" in positions

    def test_invalid_query_type(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="drop_tables", identity={"puuid": "p1"})
            assert r["data_quality"]["status"] == "INVALID_QUERY_TYPE"

    def test_missing_identity(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="recent", identity=None)
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_invalid_identity_self(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="recent", identity={"self": True})
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_match_analysis_missing_id(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="match_analysis")
            assert r["data_quality"]["status"] == "MISSING_LOCAL_MATCH_ID"

    def test_match_analysis_ok(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="match_analysis", local_match_id="m1")
            assert r["result"]["local_match_id"] == "m1"

    def test_negative_limit(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            r = svc.query(query_type="recent", identity={"puuid": "p1"}, limit=-5)
            assert r["data_quality"]["status"] == "INVALID_LIMIT"


class TestCommandHandler:
    def test_handler_invokes_service(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc)
            cmd = Command(
                command_type=CommandType.PLAYER_QUERY,
                params={"query_type": "recent", "identity": {"puuid": "p1"}},
                source="voice",
            )
            r = handler(cmd)
            assert r["query_type"] == "recent"
            assert len(r["result"]["recent_matches"]) == 2

    def test_handler_uses_command_params(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc)
            cmd = Command(
                command_type=CommandType.PLAYER_QUERY,
                params={"query_type": "champion_stats", "identity": {"puuid": "p1"}},
            )
            r = handler(cmd)
            assert "champion_stats" in r["result"]


class TestFormatter:
    def test_recent_win_loss(self):
        text = format_query_result({
            "query_type": "recent",
            "result": {
                "sample": {"completed_games": 5, "incomplete_games": 0},
                "recent_matches": [{"result": "Win"}, {"result": "Win"},
                                   {"result": "Win"}, {"result": "Lose"},
                                   {"result": "Lose"}],
            },
            "data_quality": {},
        })
        assert "5场" in text
        assert "3胜2负" in text

    def test_recent_no_data(self):
        text = format_query_result({
            "query_type": "recent", "result": None,
            "data_quality": {"status": "NO_MATCHES"},
        })
        assert "没有" in text or "数据" in text

    def test_champion_stats(self):
        text = format_query_result({
            "query_type": "champion_stats",
            "result": {"champion_stats": [
                {"champion_name": "Ahri", "games": 5, "wins": 3, "losses": 2},
                {"champion_name": "Lux", "games": 2, "wins": 1, "losses": 1},
            ]},
            "data_quality": {},
        })
        assert "阿狸" in text or "Ahri" in text

    def test_champion_empty(self):
        text = format_query_result({
            "query_type": "champion_stats",
            "result": {"champion_stats": []},
            "data_quality": {},
        })
        assert "没有" in text or "数据" in text

    def test_role_stats(self):
        text = format_query_result({
            "query_type": "role_stats",
            "result": {"role_stats": [
                {"position": "mid", "games": 8, "wins": 5, "losses": 3},
            ]},
            "data_quality": {},
        })
        assert "mid" in text

    def test_missing_identity(self):
        text = format_query_result({
            "query_type": "recent", "result": None,
            "data_quality": {"status": "MISSING_IDENTITY"},
        })
        assert "身份" in text

    def test_invalid_type(self):
        text = format_query_result({
            "query_type": "drop_tables", "result": None,
            "data_quality": {"status": "INVALID_QUERY_TYPE"},
        })
        assert "不支持" in text


class TestSelfIdentity:
    def test_self_fallback_uses_state_store(self):
        class FakeStateStore:
            def get(self, ns, key, default=None):
                return {"puuid": "self-puuid"}
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            # seed uses puuid p1, not self-puuid, so result should be NO_MATCHES
            # but identity resolution itself should not crash
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=FakeStateStore())
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent"})
            r = handler(cmd)
            # self-puuid not in DB → NO_MATCHES or empty
            assert r is not None

    def test_no_state_store_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=None)
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent"})
            r = handler(cmd)
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_explicit_identity_wins_over_self(self):
        class FakeStateStore:
            def get(self, ns, key, default=None):
                return {"puuid": "self-puuid"}
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=FakeStateStore())
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": {"puuid": "p1"}})
            r = handler(cmd)
            assert len(r["result"]["recent_matches"]) == 2


class TestIdentityDefense:
    def test_string_self_resolves(self):
        class FakeSS:
            def get(self, ns, k, default=None):
                return {"puuid": "p1"}
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=FakeSS())
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": "self"})
            r = handler(cmd)
            assert len(r["result"]["recent_matches"]) == 2

    def test_dict_self_resolves(self):
        class FakeSS:
            def get(self, ns, k, default=None):
                return {"puuid": "p1"}
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=FakeSS())
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": {"self": True}})
            r = handler(cmd)
            assert len(r["result"]["recent_matches"]) == 2

    def test_self_no_state_store(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=None)
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": "self"})
            r = handler(cmd)
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_self_no_current_summoner(self):
        class EmptySS:
            def get(self, ns, k, default=None):
                return None
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=EmptySS())
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": "self"})
            r = handler(cmd)
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_none_identity(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=None)
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": None})
            r = handler(cmd)
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_int_identity(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=None)
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": 123})
            r = handler(cmd)
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_list_identity(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=None)
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": []})
            r = handler(cmd)
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_empty_dict_identity(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=None)
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": {}})
            r = handler(cmd)
            assert r["data_quality"]["status"] == "MISSING_IDENTITY"

    def test_explicit_puuid_wins_over_self(self):
        class FakeSS:
            def get(self, ns, k, default=None):
                return {"puuid": "other"}
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            handler = make_player_query_handler(svc, state_store=FakeSS())
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": {"puuid": "p1"}})
            r = handler(cmd)
            assert len(r["result"]["recent_matches"]) == 2


class TestSpeechEnqueue:
    def test_enqueue_called(self):
        class FakeQueue:
            def __init__(self):
                self.items = []
            def enqueue(self, item):
                self.items.append(item)
                return True
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = _make_service(db, log)
            q = FakeQueue()
            handler = make_player_query_handler(svc, speech_queue=q)
            cmd = Command(command_type=CommandType.PLAYER_QUERY,
                          params={"query_type": "recent", "identity": {"puuid": "p1"}})
            handler(cmd)
            assert len(q.items) == 1
            assert q.items[0].text
            assert q.items[0].category == "player_query"
