"""A5 经济状态边沿检测回归（直接调用，无真人）。"""
import sys; sys.path.insert(0, ".")
from app.lol.snapshot import GameSnapshot, PlayerInfo
from app.player.economy_state_detector import EconomyStateDetector
from app.player.economy_advisor import make_economy_advisor

def p(name, team, gold):
    return PlayerInfo(champion_name=name, team=team, gold=gold)

def snap(diff_total):
    s = GameSnapshot()
    our = 20000 + diff_total
    s.current_player = p("A", "ORDER", our // 2)
    s.players = [s.current_player, p("B", "ORDER", our - our // 2),
                 p("C", "CHAOS", 10000), p("D", "CHAOS", 10000)]
    return s

# Test1: 初始化无播报
d = EconomyStateDetector()
assert d.update(3000) is None
print("T1 init silent OK")

# Test2: 跨越 +2000
d = EconomyStateDetector(); d.update(1800)  # init NEAR
t = d.update(2200)
assert t and "领先" in t, t
print("T2 cross +2000 ->", t)

# Test3: 同状态内不重复
for v in (2300, 2500, 3000):
    assert d.update(v) is None
print("T3 no repeat within state OK")

# Test4: 反向跨越
t = d.update(1500)
assert t and "接近" in t, t
print("T4 reverse cross ->", t)

# Test5: 跨局清理
a = make_economy_advisor(lambda: snap(3000), None, enabled=True)
a["on_time_update"](); a["on_game_reset"]()
class Q:
    def __init__(self): self.items=[]
    def enqueue(self, it): self.items.append(it)
q = Q()
a2 = make_economy_advisor(lambda: snap(-100), q, enabled=True)
a2["on_time_update"]()
assert len(q.items) == 0, q.items
print("T5 cross-game reset silent OK")

cur = {"diff": 1800}
prov = lambda: snap(cur["diff"])
q2 = Q()
a3 = make_economy_advisor(prov, q2, enabled=True)
a3["on_time_update"]()
a3["on_time_update"]()
assert len(q2.items) == 0
cur["diff"] = 2500
a3["on_time_update"]()
assert len(q2.items) == 1 and "领先" in q2.items[0].text, q2.items
cur["diff"] = 3000
a3["on_time_update"]()
assert len(q2.items) == 1, q2.items
print("T6 one enqueue on cross only OK")

# --- A5 数据边界收口：金币不可得时安全静音 ---
def snap_nogold():
    s = GameSnapshot()
    s.current_player = PlayerInfo(champion_name="A", team="ORDER")  # gold 默认0（模拟 playerlist gold 空）
    s.players = [s.current_player, PlayerInfo(champion_name="C", team="CHAOS")]
    return s

class Q3:
    def __init__(self): self.items=[]
    def enqueue(self, it): self.items.append(it)
q3=Q3()
a4 = make_economy_advisor(snap_nogold, q3, enabled=True)
for _ in range(3):
    a4["on_time_update"]()
assert len(q3.items)==0, q3.items
print("T1/T2 gold missing -> silent, no enqueue OK")

a4["on_game_reset"]()
a4["on_time_update"]()
assert len(q3.items)==0
print("T3 cross-game reset still silent OK")

from app.player.query_handler import make_player_query_handler
class C:
    def __init__(self, q): self.params=q
h = make_player_query_handler(None, speech_queue=Q3(), state_store=None, snapshot_provider=snap_nogold)
res = h(C({"query_type":"economy_diff"}))
assert ("读不到" in res["result"]) and ("接近" not in res["result"]), res
print("T4 A4 honest unknown wording OK")

print("ALL A5 OK")
import os; os._exit(0)
