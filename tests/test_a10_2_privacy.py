import sys
sys.path.insert(0, ".")
from app.lol.ongoing.summoner_privacy import SummonerPrivacyService, PUBLIC, PRIVATE, ERROR

class FakeLCU:
    def __call__(self, ep):
        if ep == "/lol-league-session/v1/league-session-token":
            return "ls-token"
        if ep == "/lol-rso-auth/v1/authorization":
            return {"currentPlatformId": "GZ100"}
        return None

# 1. 10 players, 8 PUBLIC 2 PRIVATE, shuffled order -> match by puuid
resp = []
for i in range(8):
    resp.append({"puuid": f"P{i}", "privacy": "PUBLIC"})
resp.append({"puuid": "P8", "privacy": "PRIVATE"})
resp.append({"puuid": "P9", "privacy": "PRIVATE"})
# shuffle
import random
random.shuffle(resp)
def post(url, body, headers):
    assert set(body) == {f"P{i}" for i in range(10)}, body
    return resp
svc = SummonerPrivacyService(FakeLCU(), https_post=post)
out = svc.batch_privacy([f"P{i}" for i in range(10)])
assert out["P0"] == PUBLIC and out["P7"] == PUBLIC
assert out["P8"] == PRIVATE and out["P9"] == PRIVATE
print("1 10人回填 OK")

# 2. shuffled already covered; add empty puuid filter
def post2(url, body, headers):
    assert set(body) == {"P1", "P2"}, body
    return [{"puuid": "P2", "privacy": "PRIVATE"}, {"puuid": "P1", "privacy": "PUBLIC"}]
svc2 = SummonerPrivacyService(FakeLCU(), https_post=post2)
out = svc2.batch_privacy(["P1", "", None, "  ", "P2", "P1"])
assert out == {"P1": PUBLIC, "P2": PRIVATE}, out
print("2 空puuid/去重 OK")

# 3. HTTP error -> all ERROR, no crash
def boom(*a): raise Exception("500")
svc3 = SummonerPrivacyService(FakeLCU(), https_post=boom)
out = svc3.batch_privacy(["X", "Y"])
assert out == {"X": ERROR, "Y": ERROR}, out
print("3 HTTP错误降级 OK")

# 4. token fail -> ERROR without crashing
class NoTok(FakeLCU):
    def __call__(self, ep):
        if ep == "/lol-league-session/v1/league-session-token": return None
        if ep == "/lol-rso-auth/v1/authorization": return {"currentPlatformId":"GZ100"}
        return None
svc4 = SummonerPrivacyService(NoTok(), https_post=lambda *a: [{"puuid":"X","privacy":"PUBLIC"}])
assert svc4.batch_privacy(["X"]) == {"X": ERROR}
print("4 token失败 OK")

# 5. empty response [] -> all ERROR (not PRIVATE)
svc5 = SummonerPrivacyService(FakeLCU(), https_post=lambda *a: [])
assert svc5.batch_privacy(["Q1"]) == {"Q1": ERROR}
print("5 空响应 OK")

# 6. unknown privacy string -> ERROR not PRIVATE
svc6 = SummonerPrivacyService(FakeLCU(), https_post=lambda *a: [{"puuid":"Q2","privacy":"WHATEVER"}])
assert svc6.batch_privacy(["Q2"]) == {"Q2": ERROR}
print("6 未知privacy OK")

print("ALL A10.2 batch privacy tests OK")
