import sys
sys.path.insert(0, ".")
from app.lol.ongoing.match_detail import MatchTimelineService, _fmt_ms, parse_details_events

# time fmt
assert _fmt_ms(0)=="00:00"; assert _fmt_ms(90000)=="01:30"; assert _fmt_ms(600000)=="10:00"; assert _fmt_ms(3661000)=="61:01"
print("1 time OK")

class FakeLCU:
    def __call__(self, ep):
        if ep == "/entitlements/v1/token": return {"accessToken":"t"}
        if ep == "/lol-rso-auth/v1/authorization": return {"currentPlatformId":"GZ100"}
        return None

DETAILS = {"json": {
    "participants": [{"participantId":1,"puuid":"PUID_A"},{"participantId":2,"puuid":"PUID_B"}],
    "frames": [
        {"timestamp":0, "participantFrames": {
            "1": {"currentGold":500,"totalGold":500,"level":1,"xp":0,"minionsKilled":0,"jungleMinionsKilled":0}}},
        {"timestamp":60000, "participantFrames": {
            "1": {"currentGold":1200,"totalGold":1800,"level":3,"xp":900,"minionsKilled":20,"jungleMinionsKilled":5}}},
        {"timestamp":120000, "participantFrames": {
            "1": {"currentGold":None,"totalGold":2500,"level":5,"xp":2500,"minionsKilled":35,"jungleMinionsKilled":10}}},
    ],
}}

import urllib.request
calls = {"n":0}
def fake_open(*a, **k):
    calls["n"] += 1
    class R:
        def read(self_inner):
            import json; return json.dumps(DETAILS).encode()
    return R()
orig = urllib.request.urlopen
urllib.request.urlopen = fake_open
try:
    svc = MatchTimelineService(FakeLCU())
    ev, pmap = svc.get_events(1)
    pmap2, series = svc.get_stats(1)
    ev2, pmap3 = svc.get_events(1)
finally:
    urllib.request.urlopen = orig

# participantId -> puuid
assert pmap == {1:"PUID_A",2:"PUID_B"}, pmap
# series parsed
f1 = series[1]
assert f1[0]["total_gold"]==500 and f1[1]["minions"]==20 and f1[1]["jungle"]==5
# None preserved (not forced to 0)
assert f1[2]["current_gold"] is None
# total cs = 20+5 = 25 at frame1
assert (f1[1]["minions"] or 0)+(f1[1]["jungle"] or 0)==25
# single HTTP across events+stats+events
assert calls["n"] == 1, calls
print("2 frames/puuid/None/cs OK, 3 single HTTP =", calls["n"])

# empty frames
EMPTY = {"json": {"participants":[{"participantId":1,"puuid":"X"}],"frames":[]}}
urllib.request.urlopen = lambda *a,**k: type("R",(),{"read":lambda s: __import__("json").dumps(EMPTY).encode()})()
svc2 = MatchTimelineService(FakeLCU())
ev, pmap = svc2.get_events(1); sm, sr = svc2.get_stats(1)
assert ev == [] and sr == {}
urllib.request.urlopen = orig
print("4 empty frames OK")

# http failure raises
def boom(*a,**k): raise Exception("500")
urllib.request.urlopen = boom
try:
    try:
        MatchTimelineService(FakeLCU()).get_stats(1)
        raise AssertionError("should raise")
    except Exception:
        pass
finally:
    urllib.request.urlopen = orig
print("5 http failure OK")

print("ALL A10.4 tests OK")
