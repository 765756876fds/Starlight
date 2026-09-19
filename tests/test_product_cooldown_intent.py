"""产品复查：静态冷却查询 vs 历史查询 Intent 不抢。"""
import sys
sys.path.insert(0, ".")
from app.voice.intent import RuleBasedIntent as R

def route(t):
    i = R.match(t)
    return (i.intent_type, i.params) if i else None

# T-C1..C2 静态冷却
assert route("石头人的大招冷却时间是多少") == ("cooldown.query", {"champion": "石头人", "skill": "大招"})
print("T-C1 石头人 R static OK")
assert route("墨菲特R冷却多久") == ("cooldown.query", {"champion": "墨菲特", "skill": "R"})
print("T-C2 墨菲特 R static OK")

# T-C3/C4 不被历史/cooldown 互抢（这两句历史查询在规则层不命中静态CD；不抛错、不进 cooldown.query）
r1 = route("我最近玩得最多的英雄是什么")
assert r1 is None or r1[0] != "cooldown.query", r1
print("T-C3 history not hijacked by cooldown OK", r1)
r2 = route("石头人的大招冷却时间是多少秒")
assert r2 and r2[0] == "cooldown.query", r2
print("T-C4 cooldown not hijacked by history OK")

# 召唤师技能静态
assert route("闪现多久")[0] == "cooldown.query"
print("T-C5 summoner static OK")

print("ALL intent regression OK")
