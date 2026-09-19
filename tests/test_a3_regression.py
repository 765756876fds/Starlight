"""A3 关键模拟回归（直接调用，无 pytest/无真人）。"""
import sys; sys.path.insert(0, ".")
from app.lol.snapshot import GameSnapshot, PlayerInfo
from app.player.current_game_analysis import richest, self_kda

def snap():
    s = GameSnapshot()
    s.current_player = PlayerInfo(champion_name="A", team="ORDER", gold=8000, level=10, cs=100, kills=7, deaths=2, assists=5)
    s.players = [
        s.current_player,
        PlayerInfo(champion_name="B", team="ORDER", gold=10000, level=9, cs=80),
        PlayerInfo(champion_name="C", team="ORDER", gold=7000, level=11, cs=120),
        PlayerInfo(champion_name="D", team="CHAOS", gold=12000, level=10, cs=100),
        PlayerInfo(champion_name="E", team="CHAOS", gold=9000, level=12, cs=110),
    ]
    return s

# Test1: 对面谁最肥 = D (gold最高)
r = richest(snap(), "enemy")
assert r["champion"] == "D" and r["gold"] == 12000, r
print("T1 enemy richest = D (gold 12000) OK")

# Test2: self KDA = 7/2/5
k = self_kda(snap())
assert (k["kills"], k["deaths"], k["assists"]) == (7, 2, 5), k
print("T2 self kda 7/2/5 OK")

# Test4: 当前局/历史局隔离 —— 纯函数，snapshot 不同结果不同
sa = snap()
sb = GameSnapshot()
sb.current_player = PlayerInfo(champion_name="A", team="ORDER", kills=1, deaths=3, assists=0)
sb.players = [sb.current_player, PlayerInfo(champion_name="D", team="CHAOS", gold=6000)]
kb = self_kda(sb)
assert (kb["kills"], kb["deaths"], kb["assists"]) == (1, 3, 0), kb
assert richest(sb, "enemy")["gold"] == 6000
assert "A的旧数据不会串到B" and (self_kda(sa)["kills"] == 7)
print("T4 current/history isolation OK (B=1/3/0, A still 7/2/5)")

# Test5: 生命周期幂等 —— MatchRepository INSERT OR IGNORE + finish UPDATE 幂等
import tempfile, os
from app.player.match_repository import MatchRepository
tmp = os.path.join(tempfile.gettempdir(), "a3_idem.db")
if os.path.exists(tmp): os.remove(tmp)
repo = MatchRepository(tmp)
repo.create_match("A", 100.0, "PRACTICETOOL", "Map11")
repo.create_match("A", 100.0, "PRACTICETOOL", "Map11")  # dup GAME_STARTED
repo.save_match_players("A", [{"riot_id":"D","team":"CHAOS","champion_name":"D","is_self":False}])
repo.save_match_players("A", [{"riot_id":"D","team":"CHAOS","champion_name":"D","is_self":False}])  # dup save
repo.finish_match("A", 200.0, 100.0, "Win")
repo.finish_match("A", 200.0, 100.0, "Win")  # dup GAME_ENDED
m = repo.get_match("A")
assert repo.count_matches() == 1, repo.count_matches()
assert len(m["players"]) == 1, m["players"]
assert m["match"]["duration"] == 100.0
repo.create_match("B", 300.0, "PRACTICETOOL", "Map11")
assert repo.count_matches() == 2
print("T5 idempotent lifecycle: 1 match / 1 player / B new OK")

# Test A: 空 snapshot
assert richest(GameSnapshot(), "enemy") is None
assert self_kda(GameSnapshot()) is None
print("T-A empty snapshot -> None OK")

# Test B: 缺字段不崩溃（gold 默认0）
sg = GameSnapshot()
sg.current_player = PlayerInfo(champion_name="X", team="ORDER")
sg.players = [sg.current_player, PlayerInfo(champion_name="Y", team="CHAOS")]
assert richest(sg, "enemy")["champion"] == "Y"
assert self_kda(sg)["kills"] == 0
print("T-B missing fields OK")

# Test3: 连续 player.query 不被 dedup —— 用 fake speech queue + handler
from app.player.query_handler import make_player_query_handler
class FakeSpeech:
    def __init__(self): self.items = []
    def enqueue(self, item): self.items.append(item)
fs = FakeSpeech()
handler = make_player_query_handler(None, speech_queue=fs, state_store=None,
                                    snapshot_provider=snap)
for qt, side in [("current_richest","enemy"),("current_self_kda",None),("current_richest","self")]:
    class C: params={"query_type":qt} | ({"side":side} if side else {})
    handler(C())
assert len(fs.items) == 3, len(fs.items)
ids = [i.event_id for i in fs.items]
assert all(x == "" for x in ids), ids  # 空 event_id -> 不触发 dedup
print("T3 consecutive queries: 3 enqueued, no dedup collapse OK")

print("ALL A3 REGRESSION OK")
import os; os._exit(0)
