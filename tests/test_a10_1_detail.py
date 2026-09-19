import sys
sys.path.insert(0, ".")
from app.lol.ongoing.match_detail import MatchDetailService, _parse_perks

# fake lcu: entitlements token + rso platform
class FakeLCU:
    def __call__(self, ep):
        if ep == "/entitlements/v1/token":
            return {"accessToken": "tok"}
        if ep == "/lol-rso-auth/v1/authorization":
            return {"currentPlatformId": "GZ100"}
        return None

svc = MatchDetailService(FakeLCU())

# intercept HTTP
def fake_urlopen(req, context=None, timeout=20):
    class R:
        def read(self_inner):
            return __import__("json").dumps({"json": {"gameId": 12345, "participants": [
                {
                    "puuid": "ME", "gameName": "Me", "championName": "Ahri", "teamId": 100,
                    "teamPosition": "MIDDLE", "win": True,
                    "kills": 5, "deaths": 2, "assists": 8,
                    "totalMinionsKilled": 100, "neutralMinionsKilled": 20, "goldEarned": 13000,
                    "item0": 3152, "item1": 3040, "item2": 0, "item3": 0, "item4": 0, "item5": 0, "item6": 0,
                    "spell1Id": 4, "spell2Id": 14,
                    "champLevel": 16, "doubleKills": 2, "tripleKills": 0, "quadraKills": 0, "pentaKills": 0,
                    "visionScore": 18, "wardsPlaced": 7, "wardsKilled": 3,
                    "totalDamageDealtToChampions": 22000, "totalDamageTaken": 18000,
                    "magicDamageDealtToChampions": 15000, "physicalDamageDealtToChampions": 6000, "trueDamageDealtToChampions": 1000,
                    "perks": {
                        "statPerks": {"offense": 5008, "flex": 5003, "defense": 5002},
                        "styles": [
                            {"description": "primaryStyle", "style": 8200, "selections": [{"perk": 8214, "var1":0,"var2":0,"var3":0},
                                                                                            {"perk": 8226,"var1":0,"var2":0,"var3":0}]},
                            {"description": "subStyle", "style": 8100, "selections": [{"perk": 8139,"var1":0,"var2":0,"var3":0}]},
                        ],
                    },
                }
            ]}}).encode()
    return R()

import app.lol.ongoing.match_detail as md
import urllib.request
orig = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
try:
    det = svc.get(12345, "ME")
finally:
    urllib.request.urlopen = orig

p = det.players[0]
assert p.cs == 120, p.cs
assert p.level == 16
assert p.items[0] == 3152 and p.items[1] == 3040 and p.items[6] == 0
assert p.spell1 == 4 and p.spell2 == 14
assert p.vision_score == 18 and p.wards_placed == 7 and p.wards_killed == 3
assert p.dmg_to_champ == 22000 and p.dmg_taken == 18000
assert p.magic_dmg == 15000
assert p.primary_style == 8200 and p.secondary_style == 8100
assert p.primary_perks == [8214, 8226] and p.secondary_perks == [8139]
assert p.stat_perks == [5008, 5003, 5002]
print("A10.1 parser OK")
