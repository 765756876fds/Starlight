"""A1 近场战绩 parser / 失败语义回归。

阶段1 重构后：loader._fetch_history(puuid) 统一走
AccountMatchHistoryService（SGP primary, LCU fallback），
返回 MatchHistoryResult(state, records)。本文件把原 _fetch_matches
旧语义（(records, state)）迁移到新语义，保留 T-R1~T-R14 回归意图。
"""
import sys
import json

sys.path.insert(0, ".")
from app.lol.ongoing.loader import OngoingGameLoader
from app.lol.ongoing.account_history import AccountMatchHistoryService


def mkloader(getter):
    acct = AccountMatchHistoryService(lcu_get=getter)
    return OngoingGameLoader(lcu_get=getter, account_history_service=acct)


def own_stats(puuid, win, k, d, a):
    return {"puuid": puuid, "stats": {"win": win, "kills": k, "deaths": d, "assists": a}}


def fetch(getter, puuid="ME"):
    return mkloader(getter)._fetch_history(puuid)


# T-R1: 正常解析，按 puuid 匹配自己，3 胜 2 负
def fake_get(path):
    if "match-history" in path:
        games = []
        for win in (True, True, True, False, False):
            games.append({"championId": 6,
                          "json": json.dumps({"participants": [own_stats("ME", win, 5, 2, 7)]})})
        return {"games": {"games": games}}
    return {}


r = fetch(fake_get)
assert r.state == "AVAILABLE" and len(r.records) == 5, (r.state, len(r.records))
assert sum(1 for m in r.records if m.win) == 3
print("T-R1/R6 parse by puuid OK (3W2L)")

# T-R2: KDA 聚合
assert r.records[0].kills == 5 and r.records[0].deaths == 2 and r.records[0].assists == 7
print("T-R2 KDA fields OK")

# T-R7: deaths=0
def g2(path):
    return {"games": {"games": [{"json": json.dumps({"participants": [own_stats("ME", True, 10, 0, 5)]})}]}}


r = fetch(g2)
assert r.state == "AVAILABLE" and r.records[0].deaths == 0
print("T-R7 deaths=0 OK")

# T-R3: HTTP failure -> ERROR，绝不 0/10
r = fetch(lambda p: None)
assert r.state == "ERROR" and r.records == [], (r.state, r.records)
print("T-R3 HTTP fail -> ERROR OK")

# T-R4: 200 + 非 dict -> ERROR
r = fetch(lambda p: "not json")
assert r.state == "ERROR"
print("T-R4 bad payload -> ERROR OK")

# T-R5: 空列表 -> NO_DATA（与失败区分）
r = fetch(lambda p: {"games": {"games": []}})
assert r.state == "NO_DATA" and r.records == []
print("T-R5 empty -> NO_DATA OK")

# T-R11: json 是 HTML 字符串 -> 单条跳过，不伪造成绩，其余正常条仍可解析
def ghtml(path):
    games = [
        {"championId": 1, "json": "<html>captcha</html>"},
        {"championId": 2, "json": json.dumps({"participants": [own_stats("ME", True, 2, 1, 3)]})},
    ]
    return {"games": {"games": games}}


r = fetch(ghtml)
assert r.state == "AVAILABLE" and len(r.records) == 1, (r.state, len(r.records))
print("T-R11 html body skipped, valid games kept OK")

# T-R14: 真实全败（10 条都 win=False）-> 真 0 胜，state AVAILABLE
def gloss(path):
    return {"games": {"games": [{"json": json.dumps({"participants": [own_stats("ME", False, 1, 3, 1)]})} for _ in range(10)]}}


r = fetch(gloss)
assert r.state == "AVAILABLE" and sum(m.win for m in r.records) == 0 and len(r.records) == 10
print("T-R14 real 0W/10L distinguishable from NO_DATA OK")

# T-R9: 不相关 puuid 不参与（别人数据不会冒充自己）
def gother(path):
    return {"games": {"games": [{"json": json.dumps({"participants": [own_stats("OTHER", True, 99, 0, 99)]})}]}}


r = fetch(gother)
assert r.records == [], r.records
print("T-R9 foreign participant skipped OK")

print("ALL A1 ongoing regression OK")
