"""A4 确定性态势分析回归（直接调用，无真人/无pytest）。"""
import sys; sys.path.insert(0, ".")
from app.lol.snapshot import GameSnapshot, PlayerInfo
from app.player.current_game_analysis import richest, self_kda, economy, team_rankings, team_kda

def p(name, team, gold, level=10, cs=100, k=0, d=0, a=0):
    return PlayerInfo(champion_name=name, team=team, gold=gold, level=level, cs=cs,
                      kills=k, deaths=d, assists=a)

def snap5v5():
    s = GameSnapshot()
    s.current_player = p("A", "ORDER", 8000, 10, 100, 7, 2, 5)
    s.players = [
        s.current_player,
        p("B", "ORDER", 9000),
        p("C", "ORDER", 10000),
        p("D", "ORDER", 7000),
        p("E", "ORDER", 6000),
        p("F", "CHAOS", 7000),
        p("G", "CHAOS", 8000),
        p("H", "CHAOS", 7500),
        p("I", "CHAOS", 6500),
        p("J", "CHAOS", 6000),
    ]
    return s

# Test1: economy
e = economy(snap5v5())
assert e["our_total"] == 40000, e
assert e["enemy_total"] == 35000, e
assert e["diff"] == 5000 and e["state"] == "ahead", e
print("T1 economy: our40000 enemy35000 diff+5000 ahead OK")

# Test2: richest
assert richest(snap5v5(), "self")["gold"] == 10000
assert richest(snap5v5(), "enemy")["gold"] == 8000
rk = team_rankings(snap5v5(), "enemy")
assert [x["gold"] for x in rk] == sorted([7000,8000,7500,6500,6000], reverse=True)
print("T2 richest: self10000 enemy8000, rankings sorted OK")

# Test3: KDA
k = self_kda(snap5v5()); assert (k["kills"],k["deaths"],k["assists"])==(7,2,5)
tk = team_kda(snap5v5(), "self")
assert tk["n"] == 5
print("T3 self kda 7/2/5, team our n=5 OK")

# Test4: voice query full chain via handler + fake speech
from app.player.query_handler import make_player_query_handler
class FS:
    def __init__(self): self.t=[]
    def enqueue(self, it): self.t.append(it.text)
fs = FS()
h = make_player_query_handler(None, speech_queue=fs, state_store=None, snapshot_provider=snap5v5)
class C:
    def __init__(self, q): self.params=q
for q in [{"query_type":"economy_diff"},
          {"query_type":"current_richest","side":"enemy"},
          {"query_type":"current_richest","side":"self"}]:
    h(C(q))
assert len(fs.t)==3, fs.t
assert "领先约 5000" in fs.t[0], fs.t[0]
assert "最肥" in fs.t[1]
print("T4 voice chain 3 queries spoken OK:", fs.t)

# Test5: missing/unknown
assert economy(GameSnapshot())["our_n"]==0
sbad = GameSnapshot(); sbad.current_player=p("X","ORDER",0); sbad.players=[sbad.current_player, p("Y","",0)]
eb = economy(sbad)
assert eb["enemy_n"]==0, eb   # UNKNOWN team not counted
assert richest(GameSnapshot(),"enemy") is None
print("T5 snapshot=None / UNKNOWN team not counted OK")

# Test6: cross-game isolation
sa = snap5v5()
sb = GameSnapshot(); sb.current_player=p("A","ORDER",1000,9,50,1,3,0); sb.players=[sb.current_player]
assert economy(sb)["our_total"]==1000
assert economy(sa)["our_total"]==40000  # A still intact
assert self_kda(sb)["kills"]==1
print("T6 cross-game isolation OK")

print("ALL A4 REGRESSION OK")
import os; os._exit(0)
