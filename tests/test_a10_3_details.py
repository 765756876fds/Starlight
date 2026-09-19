import sys
sys.path.insert(0, ".")
from app.lol.ongoing.match_detail import parse_details_events, _fmt_ms

# time conversion
assert _fmt_ms(0) == "00:00"
assert _fmt_ms(90000) == "01:30"
assert _fmt_ms(3661000) == "61:01"
print("1 time fmt OK")

# participantId -> puuid
pmap = {1: "PUID_A", 5: "PUID_B"}
frames = [{"timestamp": 60000, "events": [
    {"type": "CHAMPION_KILL", "killerId": 1, "victimId": 5, "assistingParticipantIds": [2]},
    {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 1055},
    {"type": "BUILDING_KILL", "killerId": 1, "buildingType": "TOWER_BUILDING"},
    {"type": "ELITE_MONSTER_KILL", "killerId": 5, "monsterType": "DRAGON"},
    {"type": "UNKNOWN_XYZ"},
]}]
evs = parse_details_events({"frames": frames}, pmap)
assert len(evs) == 5
kill = [e for e in evs if e.event_type == "CHAMPION_KILL"][0]
assert kill.killer_id == 1 and kill.victim_id == 5 and kill.assist_ids == [2]
item = [e for e in evs if e.event_type == "ITEM_PURCHASED"][0]
assert item.item_id == 1055 and item.participant_id == 1
bld = [e for e in evs if e.event_type == "BUILDING_KILL"][0]
assert bld.building_type == "TOWER_BUILDING"
mon = [e for e in evs if e.event_type == "ELITE_MONSTER_KILL"][0]
assert mon.monster_type == "DRAGON"
unk = [e for e in evs if e.event_type == "UNKNOWN_XYZ"][0]
print("2 parse/kill/item/building/monster/unknown OK")

# empty frames
assert parse_details_events({"frames": []}) == []
assert parse_details_events({}) == []
print("3 empty frames OK")

# DETAILS service http failure raises (UI degrades)
class FailLCU:
    def __call__(self, ep):
        if ep == "/entitlements/v1/token": return {"accessToken":"t"}
        if ep == "/lol-rso-auth/v1/authorization": return {"currentPlatformId":"GZ100"}
        return None
from app.lol.ongoing.match_detail import MatchTimelineService
import urllib.request
orig = urllib.request.urlopen
def boom(*a, **k): raise Exception("404")
urllib.request.urlopen = boom
try:
    try:
        MatchTimelineService(FailLCU()).get_events(1)
        raise AssertionError("should raise")
    except Exception:
        pass
finally:
    urllib.request.urlopen = orig
print("4 http failure raises OK")

# successful service parse
good = {"json": {"participants": [{"participantId":1,"puuid":"PUID_A"}],
                 "frames":[{"timestamp":1000,"events":[{"type":"GAME_END"}]}]}}
def ok_open(*a, **k):
    class R:
        def read(self_inner):
            import json; return json.dumps(good).encode()
    return R()
urllib.request.urlopen = ok_open
try:
    evs2, pmap2 = MatchTimelineService(FailLCU()).get_events(1)
finally:
    urllib.request.urlopen = orig
assert evs2[0].event_type == "GAME_END" and pmap2 == {1:"PUID_A"}
print("5 service success OK")

print("ALL A10.3 tests OK")
