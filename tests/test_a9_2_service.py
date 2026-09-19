import sys
sys.path.insert(0, ".")
from app.lol.ongoing.account_history import AccountMatchHistoryService, queue_label
from app.lol.ongoing.match_model import AVAILABLE, NO_DATA, ERROR
from app.lol.ongoing import sgp_provider as sp

def rec(ts, q, w, k=5,d=2,a=7, name="Ahri"):
    from app.lol.ongoing.match_model import MatchRecord
    return MatchRecord(game_id=ts, game_creation=ts, queue_id=q, champion_name=name, win=w,
                       kills=k, deaths=d, assists=a, source="sgp")

# T sorting: unsorted -> DESC
s = AccountMatchHistoryService(lambda ep: None)
r = s._finalize(type("R",(),{"state":AVAILABLE,"records":[rec(100,420,True),rec(300,420,False),rec(200,420,True)]})())
assert [x.game_creation for x in r.records] == [300,200,100], "sort"
assert r.records[0].queue_type == "单双排位"
print("T sort+queue label OK")

# T queue map
assert queue_label(3140)=="新手/训练"; assert queue_label(870)=="人机"; assert queue_label(9999)=="未知模式"
print("T queue map OK")

# T no puuid -> ERROR
s = AccountMatchHistoryService(lambda ep: None)
assert s.recent_matches().state == ERROR
print("T no puuid OK")

# T SGP success path (fake lcu)
def fake_lcu(ep):
    if ep=="/lol-summoner/v1/current-summoner": return {"puuid":"ME"}
    if ep=="/entitlements/v1/token": return {"accessToken":"tok"}
    if ep=="/lol-rso-auth/v1/authorization": return {"currentPlatformId":"HN10"}
fake = {
  "games":[{"json":{"gameId":1,"gameCreation":200,"queueId":420,
    "participants":[{"puuid":"ME","championName":"Ahri","win":True,"kills":1,"deaths":0,"assists":2,"totalMinionsKilled":10,"goldEarned":5000}]}}]}
sp.SgpMatchHistoryProvider.recent = lambda self,p,start_index=0,count=20: sp.MatchHistoryResult(state=AVAILABLE, records=[rec(200,420,True)])
s = AccountMatchHistoryService(fake_lcu)
r = s.recent_matches()
assert r.state==AVAILABLE and len(r.records)==1 and r.records[0].queue_type=="单双排位"
print("T SGP path OK")

# T fallback: SGP fails -> LCU
sp.SgpMatchHistoryProvider.recent = lambda self,p,start_index=0,count=20: sp.MatchHistoryResult(state=ERROR)
def lcu2(ep):
    if ep=="/lol-summoner/v1/current-summoner": return {"puuid":"ME"}
    if ep=="/entitlements/v1/token": return {"accessToken":"tok"}
    if ep=="/lol-rso-auth/v1/authorization": return {"currentPlatformId":"HN10"}
    if ep.startswith("/lol-match-history/v1"):
        return {"games":{"games":[{"json":{"gameId":9,"gameCreation":100,"queueId":870,
          "participants":[{"puuid":"ME","championName":"Ashe","win":False,"kills":0,"deaths":1,"assists":0}]}}]}}
s = AccountMatchHistoryService(lcu2)
r = s.recent_matches()
assert r.state==AVAILABLE and r.records[0].source=="lcu" and r.records[0].queue_type=="人机"
print("T LCU fallback OK")

print("ALL A9.2 tests OK")
