"""A9 SGP Provider / 统一模型 stub 测试。"""
import sys, json
sys.path.insert(0, ".")
from app.lol.ongoing.sgp_provider import (
    SgpMatchHistoryProvider, parse_sgp_summary, tencent_sgp_base_from_platform,
)
from app.lol.ongoing.match_model import AVAILABLE, NO_DATA, AUTH_REQUIRED, SGP_UNAVAILABLE


def game(puuid, win, k, d, a, gold=10000, cs=200):
    return {"json": {
        "gameId": 1000, "gameCreation": 1700000000000, "gameDuration": 1800,
        "queueId": 420, "participants": [
            {"puuid": "OTHER", "championId": 1, "win": not win,
             "kills": 0, "deaths": 0, "assists": 0, "goldEarned": 1,
             "totalMinionsKilled": 0, "neutralMinionsKilled": 0, "teamId": 200,
             "championName": "Other", "teamPosition": ""},
            {"puuid": puuid, "championId": 6, "championName": "Ahri", "win": win,
             "kills": k, "deaths": d, "assists": a, "goldEarned": gold,
             "totalMinionsKilled": cs, "neutralMinionsKilled": cs,
             "teamPosition": "middle", "teamId": 100},
        ]}}

# T: parser 正常
payload = {"games": [game("ME", True, 5, 2, 7), game("ME", False, 1, 8, 2)]}
r = parse_sgp_summary(payload, "ME")
assert r.state == AVAILABLE and len(r.records) == 2, r.state
assert r.records[0].win is True and (r.records[0].kills, r.records[0].deaths, r.records[0].assists) == (5, 2, 7)
assert r.records[0].cs == 400 and r.records[0].champion_name == "Ahri"
print("T parser/identity/puuid-match OK")

# T: 空 -> NO_DATA
assert parse_sgp_summary({"games": []}, "ME").state == NO_DATA
from app.lol.ongoing.match_model import ERROR
assert parse_sgp_summary("junk", "ME").state == ERROR
print("T empty/bad -> NO_DATA/ERROR OK")

# T: 别人 participant 不冒充
payload_other = {"games": [game("X", True, 99, 0, 99)]}
r = parse_sgp_summary(payload_other, "ME")
assert r.state == NO_DATA, r.state
print("T foreign participant skipped OK")

# T: provider token/base 缺失
p = SgpMatchHistoryProvider(lambda: None, lambda: "https://x")
assert p.recent("ME").state == AUTH_REQUIRED
p = SgpMatchHistoryProvider(lambda: "t", lambda: None)
assert p.recent("ME").state == SGP_UNAVAILABLE
print("T auth/base missing states OK")

# T: provider 成功（mock https）
def fake_https(url, headers):
    assert "x-akari-token-type" in headers and headers["Authorization"] == "Bearer t"
    assert "/SUMMARY?startIndex=0&count=20" in url
    return payload
p = SgpMatchHistoryProvider(lambda: "t", lambda: "https://sgp", https_get=fake_https)
r = p.recent("ME", start_index=0, count=20)
assert r.state == AVAILABLE and len(r.records) == 2
print("T provider success OK")

# T: 请求异常 -> SGP_UNAVAILABLE
def boom(url, headers): raise RuntimeError("net")
p = SgpMatchHistoryProvider(lambda: "t", lambda: "https://x", https_get=boom)
assert p.recent("ME").state == SGP_UNAVAILABLE
print("T request error -> SGP_UNAVAILABLE OK")

# T: 腾讯 base 映射
assert tencent_sgp_base_from_platform("HN10").startswith("https://hn10")
assert tencent_sgp_base_from_platform("XX") is None
print("T tencent base map OK")

print("ALL A9 SGP tests OK")
