"""A8 stability/stub tests: malformed 2999 payload + sqlite boundary（不启 Qt/游戏）。"""
import os, sys, tempfile
sys.path.insert(0, ".")

from app.lol.snapshot import SnapshotBuilder

def build(all_data):
    return SnapshotBuilder.from_api_data(all_data)

# T1: 正常 payload
s = build({"gameData": {"gameTime": 123.0, "gameMode": "PRACTICETOOL"},
           "allPlayers": [{"summonerName": "self#1", "team": "ORDER", "championName": "A",
                           "scores": {"kills": 1, "deaths": 0, "assists": 2, "creepScore": 10}}],
           "activePlayer": {"summonerName": "self#1"},
           "events": {"Events": [{"EventName": "GameStart"}]}})
assert s.game_time == 123.0 and len(s.recent_events) == 1
print("T1 normal payload OK")

# T2: events 是字符串（触发原 'str'.get() 形态）
s = build({"gameData": {"gameTime": 1.0}, "allPlayers": [], "events": "not-a-dict"})
assert s.recent_events == [], s.recent_events
print("T2 events=str no crash OK")

# T3: events 是 list（非 {"Events":[]}）
s = build({"gameData": {"gameTime": 1.0}, "allPlayers": [], "events": [{"EventName": "Foo"}]})
assert s.recent_events == [{"EventName": "Foo"}]
print("T3 events=list OK")

# T4: 缺字段
s = build({})
assert s.game_time == 0.0 and s.players == [] and s.recent_events == []
print("T4 missing fields OK")

# T5: all_data None / 空
s = build(None)
assert s.players == []
print("T5 None OK")

# T6: 空 dict（已同 T4，显式）
s = build({})
print("T6 empty dict OK")

# sqlite 边界（T7/T8 重复事件、T10 空库）
from app.player.match_repository import MatchRepository
from app.player.history_repository import HistoryRepository
db = os.path.join(tempfile.mkdtemp(), "x.db")
mr, hr = MatchRepository(db), HistoryRepository(db)
mr.create_match("g1", 1.0, "P", "M"); mr.create_match("g1", 1.0, "P", "M")  # T7 dup start
mr.finish_match("g1", 2.0, 1.0, "Win"); mr.finish_match("g1", 2.0, 1.0, "Win")  # T8 dup end
assert hr.get_match("g1")["match"]["result"] == "Win"
assert len(hr.get_recent_matches()) == 1
print("T7/T8 duplicate start/end idempotent OK")

# T10: 空库查询（全新空库）
hr2 = HistoryRepository(os.path.join(tempfile.mkdtemp(), "empty.db"))
assert hr2.get_match("nope") is None
assert hr2.get_recent_matches() == []
assert hr2.get_player_matches(riot_id="nobody") == []
print("T10 empty db OK")

print("ALL A8 OK")
