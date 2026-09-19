"""P1 killer_team + P2 champion/skill 切分 直接调用 stub。"""
import sys; sys.path.insert(0, ".")

# ---- P2: Intent ----
from app.voice.intent import RuleBasedIntent
def p2(text):
    i = RuleBasedIntent.match(text)
    if not i: return (None, None)
    return (i.params.get("champion"), i.params.get("skill"))

cases = [
    "对面石头人 W 了", ("石头人", "W"),
    "对面石头人，W了", ("石头人", "W"),
    "石头人W用了", ("石头人", "W"),
    "石头人的W用了", ("石头人", "W"),
    "对面石头人 R 了", ("石头人", "R"),
    "对面石头人，R了", ("石头人", "R"),
    "石头人R用了", ("石头人", "R"),
    "石头人的R用了", ("石头人", "R"),
    "对面石头人，R大招用了", ("石头人", "R"),
    "对面石头人R大招用了", ("石头人", "R"),
]
ok = True
for i in range(0, len(cases), 2):
    txt, expect = cases[i], cases[i+1]
    got = p2(txt)
    flag = "OK" if got == expect else "FAIL"
    if got != expect: ok = False
    print(f"  [{flag}] {txt!r} -> champion={got[0]!r} skill={got[1]!r} (want {expect})")

# music/query regression unchanged
q = RuleBasedIntent.match("石头人大招冷却多久")
print("  query:", q.params if q else None)
m = RuleBasedIntent.match("下一首")
print("  music next:", m.intent_type if m else None)

# ---- P1: _find_team ----
from app.lol.state_diff import StateDiff
from app.lol.snapshot import GameSnapshot, PlayerInfo
sd = StateDiff.__new__(StateDiff)
snap = GameSnapshot()
snap.current_player = PlayerInfo(summoner_name="谷丶小强", champion_name="卡尔萨斯", team="ORDER")
snap.players = [
    PlayerInfo(summoner_name="谷丶小强", champion_name="卡尔萨斯", team="ORDER"),
    PlayerInfo(summoner_name="bot1", champion_name="荆棘之兴", team="CHAOS"),
]
assert sd._find_team(snap, "谷丶小强") == "ORDER", "self -> ORDER"
assert sd._find_team(snap, " 谷丶小强 ") == "ORDER", "strip"
assert sd._find_team(snap, "荆棘之兴") == "CHAOS", "bot -> CHAOS"
assert sd._find_team(snap, "不存在的人") == "UNKNOWN", "unknown"
print("  P1 self->ORDER, bot->CHAOS, unknown->UNKNOWN OK")

# ---- Soul count via tracker ----
from app.lol.objectives.rules import load_ruleset
from app.lol.objectives.tracker import ObjectiveTracker
tr = ObjectiveTracker(load_ruleset()); tr.on_game_start()
for _ in range(3):
    tr.on_dragon_killed(600.0, "", "ORDER")
tr.on_dragon_killed(700.0, "", "ORDER")  # 4th
assert tr._soul_team == "ORDER", "4 ORDER -> soul"
assert abs(tr.next_spawn_times()["elder"] - 1060) < 1, "elder = kill+360"
tr2 = ObjectiveTracker(load_ruleset()); tr2.on_game_start()
for _ in range(5):
    tr2.on_dragon_killed(600.0, "", "UNKNOWN")
assert tr2._soul_team is None, "UNKNOWN no soul"
print("  P1 soul: ORDER=4 -> elder OK; UNKNOWN never counts OK")

print("ALL P1/P2", "PASS" if ok else "FAIL")
import os; os._exit(0)


