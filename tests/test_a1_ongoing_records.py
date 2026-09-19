"""A1 近场战绩 parser / 失败语义回归。"""
import sys, json
sys.path.insert(0, ".")
from app.lol.ongoing.loader import OngoingGameLoader


def mkloader(getter):
    return OngoingGameLoader(lcu_get=getter)


def own_stats(puuid, win, k, d, a):
    return {"puuid": puuid, "stats": {"win": win, "kills": k, "deaths": d, "assists": a}}


# T-R1: 正常解析，按 puuid 匹配自己，3 胜 7 负（这里 3W2L）
def fake_get(path):
    if "match-history" in path:
        games = []
        for win in (True, True, True, False, False):
            games.append({"championId": 6,
                          "json": json.dumps({"participants": [own_stats("ME", win, 5, 2, 7)]})})
        return {"games": {"games": games}}
    return {}
L = mkloader(fake_get)
ms, st = L._fetch_matches("ME")
assert st == "AVAILABLE" and len(ms) == 5, (st, len(ms))
assert sum(1 for m in ms if m.win) == 3
print("T-R1/R6 parse by puuid OK (3W2L)")

# T-R2: KDA 聚合
assert ms[0].kills == 5 and ms[0].deaths == 2 and ms[0].assists == 7
print("T-R2 KDA fields OK")

# T-R7: deaths=0
def g2(path):
    return {"games": {"games": [{"json": json.dumps({"participants": [own_stats("ME", True, 10, 0, 5)]})}]}}
ms, st = mkloader(g2)._fetch_matches("ME")
assert st == "AVAILABLE" and ms[0].deaths == 0
print("T-R7 deaths=0 OK")

# T-R3: HTTP failure -> ERROR，绝不 0/10
ms, st = mkloader(lambda p: None)._fetch_matches("ME")
assert st == "ERROR" and ms == [], (st, ms)
print("T-R3 HTTP fail -> ERROR OK")

# T-R4: 200 + 非 dict -> ERROR
ms, st = mkloader(lambda p: "not json")._fetch_matches("ME")
assert st == "ERROR"
print("T-R4 bad payload -> ERROR OK")

# T-R5: 空列表 -> NO_DATA（与失败区分）
ms, st = mkloader(lambda p: {"games": {"games": []}})._fetch_matches("ME")
assert st == "NO_DATA" and ms == []
print("T-R5 empty -> NO_DATA OK")

# T-R11: json 是 HTML 字符串 -> 解析失败 -> NO_DATA（不伪造）
def ghtml(p):
    return {"games": {"games": [{"championId": 1, "json": "<html>captcha</html>"}]}}
ms, st = mkloader(ghtml)._fetch_matches("ME")
assert ms == [], ms
print("T-R11 html body -> no fake games OK")

# T-R14: 真实全败（10 条都 win=False）-> 真 0 胜，state AVAILABLE
def gloss(p):
    return {"games": {"games": [{"json": json.dumps({"participants": [own_stats("ME", False, 1, 3, 1)]})} for _ in range(10)]}}
ms, st = mkloader(gloss)._fetch_matches("ME")
assert st == "AVAILABLE" and sum(m.win for m in ms) == 0 and len(ms) == 10
print("T-R14 real 0W/10L distinguishable from NO_DATA OK")

# T-R9: 不相关 puuid 不参与（别人数据不会冒充自己）
def gother(p):
    return {"games": {"games": [{"json": json.dumps({"participants": [own_stats("OTHER", True, 99, 0, 99)]})}]}}
ms, st = mkloader(gother)._fetch_matches("ME")
assert ms == [], ms
print("T-R9 foreign participant skipped OK")

print("ALL A1 ongoing regression OK")
