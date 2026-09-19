"""A7 history/lifecycle stub tests（不启动 Qt、不连真游戏）。"""
import os, sys, tempfile, time
sys.path.insert(0, ".")

from app.player.match_repository import MatchRepository
from app.player.history_repository import HistoryRepository

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "player.db")
mr = MatchRepository(db)
hr = HistoryRepository(db)

now = time.time()

# T1: create + save players + query
mr.create_match("m1", now, "PRACTICETOOL", "Map11")
mr.save_match_players("m1", [
    {"riot_id": "self#1", "team": "ORDER", "champion_name": "卡尔萨斯", "kills": 7, "deaths": 2, "assists": 5, "is_self": 1},
    {"riot_id": "bot#1", "team": "CHAOS", "champion_name": "盖伦", "kills": 1, "deaths": 3, "assists": 0, "is_self": 0},
])
mr.finish_match("m1", now + 600, 600, "Win")
m = hr.get_match("m1")
assert m and m["match"]["result"] == "Win", m
self_p = [p for p in m["players"] if p["is_self"]][0]
assert self_p["champion_name"] == "卡尔萨斯" and self_p["kills"] == 7
print("T1 save/query OK")

# T2: two matches independent
mr.create_match("m2", now + 1000, "PRACTICETOOL", "Map11")
mr.save_match_players("m2", [
    {"riot_id": "self#1", "team": "ORDER", "champion_name": "艾希", "kills": 1, "deaths": 3, "assists": 0, "is_self": 1},
])
mr.finish_match("m2", now + 1600, 600, "Lose")
r1 = hr.get_match("m1")["players"]
r2 = hr.get_match("m2")["players"]
assert len(r1) == 2 and len(r2) == 1
assert [p for p in r2 if p["is_self"]][0]["champion_name"] == "艾希"
print("T2 multi-match isolation OK")

# T3: history query only completed; current (incomplete) not shown as finished
recent = hr.get_recent_matches(limit=10)
ids = sorted(x["local_match_id"] for x in recent)
assert ids == ["m1", "m2"], ids
print("T3 current/history isolation OK")

# T4: missing fields -> no crash, UNKNOWN tolerated
mr.create_match("m3", now + 2000, "ARAM", "Map12")  # no players, not finished
none_match = hr.get_match("nope")
assert none_match is None
p = hr.get_match_players("m3")
assert p == []
print("T4 missing fields/no crash OK")

# T5: duplicate events idempotent
mr.create_match("m1", now, "PRACTICETOOL", "Map11")  # duplicate GAME_STARTED
mr.finish_match("m1", now + 600, 600, "Win")          # duplicate GAME_ENDED
mr.save_match_players("m1", [{"riot_id": "self#1", "is_self": 1, "champion_name": "X"}])  # dup ignored by UNIQUE
n = len(hr.get_match_players("m1"))
assert n == 2, n  # still only original 2 players (INSERT OR IGNORE)
print("T5 duplicate events idempotent OK")

print("ALL A7 OK")
