"""A3 当前对局分析 + intent 直接调用 stub。"""
import sys; sys.path.insert(0, ".")
from app.lol.snapshot import GameSnapshot, PlayerInfo
from app.player.current_game_analysis import richest, self_kda, team_rankings
from app.voice.intent import RuleBasedIntent

# 构造一个对局：self ORDER=卡尔萨斯; enemies CHAOS
s = GameSnapshot()
s.current_player = PlayerInfo(champion_name="卡尔萨斯", team="ORDER",
                              kills=5, deaths=2, assists=3, gold=9000, level=12, cs=180)
s.players = [
    s.current_player,
    PlayerInfo(champion_name="盖伦", team="ORDER", gold=7000, level=11, cs=150),
    PlayerInfo(champion_name="墨菲特", team="CHAOS", gold=11000, level=13, cs=200),
    PlayerInfo(champion_name="艾希", team="CHAOS", gold=8000, level=12, cs=170),
]

r = richest(s, "enemy")
assert r["champion"] == "墨菲特" and r["gold"] == 11000, r
print("OK enemy richest =", r["champion"], r["gold"])

rs = richest(s, "self")
assert rs["champion"] == "卡尔萨斯" and rs["gold"] == 9000, rs
print("OK self richest =", rs["champion"])

k = self_kda(s)
assert k["kills"] == 5 and k["deaths"] == 2 and k["assists"] == 3, k
print("OK self kda =", k["kills"], k["deaths"], k["assists"])

rk = team_rankings(s, "self")
assert rk[0]["champion"] == "卡尔萨斯", rk
print("OK self ranking =", [p["champion"] for p in rk])

# empty snapshot
assert richest(GameSnapshot(), "enemy") is None
assert self_kda(GameSnapshot()) is None
print("OK empty -> None")

# intents
def intent(t):
    i = RuleBasedIntent.match(t)
    return (i.intent_type, i.params) if i else None
assert intent("对面谁最肥") == ("player.query", {"query_type": "current_richest", "side": "enemy"}), intent("对面谁最肥")
assert intent("我们谁发育最好") == ("player.query", {"query_type": "current_richest", "side": "self"})
assert intent("我现在几杀几死")[1]["query_type"] == "current_self_kda"
print("OK intents: richest enemy/self, current kda")

# regression: history intents still work
i = RuleBasedIntent.match("最近十局")
assert i.intent_type == "player.query" and i.params["query_type"] == "recent"
print("OK history intent regression")
print("ALL A3 STUB OK")
import os; os._exit(0)
