"""GameAnalysisService tests"""
import os
import json
import sqlite3
import tempfile

from app.player.match_repository import MatchRepository
from app.player.history_repository import HistoryRepository
from app.player.event_log_reader import EventLogReader
from app.player.game_analysis import GameAnalysisService


def _write_events(log_dir, events):
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "2026-09-18.jsonl"), "w", encoding="utf-8") as f:
        for i, ev in enumerate(events):
            entry = {
                "event_id": f"e{i}",
                "event_type": ev["event_type"],
                "timestamp": 1000.0 + i,
                "source": "lol",
                "payload": ev["payload"],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _seed(db, log_dir):
    mr = MatchRepository(db)
    mr.create_match("m1", 1000.0, "CLASSIC", "SR")
    mr.finish_match("m1", 1600.0, 600.0, "Win")
    mr.save_match_players("m1", [
        {"puuid": "self-p", "riot_id": "self#tag", "team": "ORDER",
         "champion_name": "Ahri", "champion_id": 103, "position": "mid",
         "kills": 5, "deaths": 2, "assists": 3, "gold": 0, "is_self": True},
        {"puuid": None, "riot_id": "foe#tag", "team": "CHAOS",
         "champion_name": "Darius", "champion_id": 1, "position": "top",
         "kills": 3, "deaths": 4, "assists": 2},
    ])
    _write_events(log_dir, [
        {"event_type": "game.first_blood", "payload": {"game_time": 132.5, "killer_name": "self#tag", "victim_name": "foe#tag", "local_match_id": "m1"}},
        {"event_type": "game.champion_kill", "payload": {"game_time": 200.0, "killer_name": "self#tag", "victim_name": "foe#tag", "local_match_id": "m1"}},
        {"event_type": "game.multi_kill", "payload": {"game_time": 250.0, "kill_streak": 2, "killer_name": "self#tag", "local_match_id": "m1"}},
        {"event_type": "game.dragon_killed", "payload": {"game_time": 400.0, "killer_name": "self#tag", "dragon_type": "Fire", "local_match_id": "m1"}},
        {"event_type": "game.tower_destroyed", "payload": {"game_time": 500.0, "killer_name": "self#tag", "local_match_id": "m1"}},
        {"event_type": "game.ace", "payload": {"game_time": 550.0, "local_match_id": "m1"}},
        # other match event, should be filtered out
        {"event_type": "game.champion_kill", "payload": {"game_time": 999.0, "killer_name": "x", "local_match_id": "OTHER"}},
    ])
    return mr


class TestNotFound:
    def test_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            assert svc.analyze_match("nope") is None


class TestFacts:
    def test_match_facts(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            a = svc.analyze_match("m1")
            assert a["match"]["result"] == "Win"
            assert a["match"]["duration"] == 600.0
            assert a["match"]["game_mode"] == "CLASSIC"

    def test_self(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            a = svc.analyze_match("m1")
            assert a["self"]["riot_id"] == "self#tag"
            assert a["self"]["puuid"] == "self-p"
            assert a["self"]["champion_name"] == "Ahri"
            assert a["self"]["kills"] == 5


class TestEvents:
    def test_counts(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            a = svc.analyze_match("m1")
            assert a["events"]["champion_kills"] == 1  # OTHER filtered out
            assert a["events"]["first_blood"] == 1
            assert a["events"]["multikills"] == 1
            assert a["events"]["dragon_killed"] == 1
            assert a["events"]["tower_destroyed"] == 1
            assert a["events"]["ace"] == 1

    def test_first_blood(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            a = svc.analyze_match("m1")
            assert a["first_blood"]["occurred"] is True
            assert a["first_blood"]["game_time"] == 132.5

    def test_multikill(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            a = svc.analyze_match("m1")
            assert a["multikills"]["double"] == 1


class TestTimeline:
    def test_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            a = svc.analyze_match("m1")
            times = [t["game_time"] for t in a["timeline"]]
            assert times == sorted(times)


class TestReadOnly:
    def test_no_db_changes(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            conn = sqlite3.connect(db)
            before_r = conn.execute("SELECT COUNT(*) FROM match_records").fetchone()[0]
            before_p = conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0]
            conn.close()

            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            svc.analyze_match("m1")

            conn = sqlite3.connect(db)
            assert conn.execute("SELECT COUNT(*) FROM match_records").fetchone()[0] == before_r
            assert conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0] == before_p
            conn.close()


class TestDeterminism:
    def test_same_result(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            _seed(db, log)
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            assert svc.analyze_match("m1") == svc.analyze_match("m1")


class TestIncomplete:
    def test_incomplete_match(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            mr = MatchRepository(db)
            mr.create_match("m-inc", 1000.0, "CLASSIC", "SR")
            # no finish
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            a = svc.analyze_match("m-inc")
            assert a is not None
            assert a["match"]["result"] == "Unknown"
            assert a["match"]["end_time"] is None


class TestSelfIdentity:
    def test_no_is_self_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "p.db")
            log = os.path.join(td, "events")
            mr = MatchRepository(db)
            mr.create_match("m-x", 1000.0, "CLASSIC", "SR")
            mr.finish_match("m-x", 1600.0, 600.0, "Win")
            # No is_self=1 on any player
            mr.save_match_players("m-x", [
                {"riot_id": "a#tag", "team": "ORDER", "champion_name": "Ahri", "is_self": 0},
                {"riot_id": "b#tag", "team": "CHAOS", "champion_name": "Darius", "is_self": 0},
            ])
            svc = GameAnalysisService(HistoryRepository(db), EventLogReader(log))
            a = svc.analyze_match("m-x")
            assert a["self"] == {
                "riot_id": None, "puuid": None, "champion_name": None,
                "champion_id": None, "team": None, "position": None,
                "kills": None, "deaths": None, "assists": None, "gold": None,
            }
            assert a["data_quality"]["self_player"] == "MISSING"
            # other fields still work
            assert a["match"]["result"] == "Win"
            assert a["match"]["duration"] == 600.0
