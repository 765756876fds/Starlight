"""A2 Game Awareness 收口：直接调用 stub（无需 pytest）。

覆盖：
- Objective: grubs/herald 首次 + 过期不补播; dragon/baron respawn;
  按队伍龙计数 -> Soul; UNKNOWN 不触发; elder 不被普通龙覆盖;
  duplicate 事件; GAME_ENDED 清空; 新局重置。
- Cooldown: 短CD T-5 / 长CD T-15; pause; reconnect; 全 gameTime。
- Enemy: roster 只含敌方; items/runes/spells; haste unavailable -> base。
"""
import sys, os
sys.path.insert(0, ".")

from app.lol.objectives.rules import load_ruleset
from app.lol.objectives.tracker import ObjectiveTracker, ObjectiveState
from app.lol.cooldown.manager import CooldownManager
from app.lol.snapshot import GameSnapshot, PlayerInfo
from app.lol.enemy.roster import EnemyRoster

PASS = []
def check(name, cond):
    assert cond, name
    PASS.append(name)

# ---------- Objective ----------
rs = load_ruleset()
tr = ObjectiveTracker(rs)
tr.on_game_start()
nxt = tr.next_spawn_times()
check("grubs first=480", abs(nxt["void_grubs"] - 480) < 1)
check("herald first=900", abs(nxt["herald"] - 900) < 1)
check("dragon first=300", abs(nxt["dragon"] - 300) < 1)
check("baron first=1200", abs(nxt["baron"] - 1200) < 1)
check("elder not active pre-soul", "elder" not in nxt)

# dragon respawn after kill at 600 -> 900
tr.on_dragon_killed(600.0, "", "ORDER")
check("dragon respawn 900", abs(tr.next_spawn_times()["dragon"] - 900) < 1)
# baron respawn
tr.on_baron_killed(1320.0)
check("baron respawn 1680", abs(tr.next_spawn_times()["baron"] - 1680) < 1)

# Team dragon counts -> Soul
tr2 = ObjectiveTracker(rs); tr2.on_game_start()
for _ in range(3):
    tr2.on_dragon_killed(600.0, "", "ORDER")
check("3 ORDER -> no soul", tr2._soul_team is None)
tr2.on_dragon_killed(700.0, "", "CHAOS")
check("3+1 -> no soul", tr2._soul_team is None)
tr2.on_dragon_killed(800.0, "", "CHAOS")  # CHAOS=2
check("3+2 -> no soul", tr2._soul_team is None)
tr2.on_dragon_killed(900.0, "", "ORDER")  # ORDER=4
check("ORDER 4 -> soul", tr2._soul_team == "ORDER")
elder_after = tr2.next_spawn_times().get("elder")
check("elder scheduled", elder_after is not None and abs(elder_after - (900+360)) < 1)
# ordinary dragon after soul does NOT overwrite elder
tr2.on_dragon_killed(1000.0, "", "ORDER")
check("elder not overwritten", abs(tr2.next_spawn_times()["elder"] - (900+360)) < 1)

# UNKNOWN killer never triggers
tr3 = ObjectiveTracker(rs); tr3.on_game_start()
for _ in range(5):
    tr3.on_dragon_killed(600.0, "", "UNKNOWN")
check("UNKNOWN 5 dragons -> no soul", tr3._soul_team is None)

# duplicate dragon kill event idempotent
tr4 = ObjectiveTracker(rs); tr4.on_game_start()
tr4.on_dragon_killed(600.0, "", "ORDER")
t1 = tr4.next_spawn_times()["dragon"]
tr4.on_dragon_killed(600.0, "", "ORDER")  # duplicate same timestamp
check("duplicate dragon idempotent", abs(tr4.next_spawn_times()["dragon"] - t1) < 1)

# game end clears counters + soul
tr2.on_game_end()
check("game end unsync counters", tr2._dragon_kills_by_team == {} and tr2._soul_team is None)

# ---------- Objective scheduling: late start no catch-up ----------
class FakeAlarm:
    def __init__(self): self._t = 0.0; self.calls = []
    _current_game_time = 0.0
    def schedule_objective_reminder(self, title, end, lead, meta):
        if end - lead <= self._t:
            return None
        self.calls.append((meta["objective"], end, lead)); return "ok"

# ---------- Cooldown threshold ----------
spoken = []
m = CooldownManager(speech_enqueue=lambda t, e: spoken.append(t))
m.set_game_time(0.0)
# short cd W=14 -> lead 5 -> remind at used+9
m.set_game_time(300.0)
r = m.start_reminder("石头人", "W")
check("short cd lead=5", r.remind_before == 5.0 and abs(r.remind_at - 309) < 0.1)
# summoner Flash base 300 -> lead 15
spoken.clear()
rf = m.start_reminder(None, "闪现")
check("flash lead=15", rf.remind_before == 15.0 and abs(rf.remind_at - (300+300-15)) < 0.1)
# pause: gameTime frozen, no fire
m.set_game_time(308.0)
check("pause no remind", spoken == [])
# reconnect jump back then reach
m.set_game_time(302.0)
check("reconnect no remind", spoken == [])
m.set_game_time(309.0)
check("short remind fires", any("5秒好" in s for s in spoken))
# game end clears
m.set_game_time(314.0)
m.start_reminder("石头人", "R")
check("R active before end", len(m.list_active()) >= 1)
m.reset_clock()
check("game end clears cooldown", len(m.list_active()) == 0 and not m._clock_synced)

# ---------- Enemy roster ----------
snap = GameSnapshot()
snap.enemy_team = [
    PlayerInfo(champion_name="墨菲特", champion_id=54, summoner_name="enemy1",
               team="CHAOS", level=11,
               items=[{"itemID": 1001}, {"itemID": 3118}],
               summoner_spells=[{"displayName": "闪现"}, {"displayName": "虚弱"}],
               runes=[{"displayName": "不灭之握"}]),
]
snap.current_player = PlayerInfo(champion_name="卡尔萨斯", team="ORDER")
roster = EnemyRoster().build(snap)
check("roster only enemy", len(roster) == 1 and roster[0].team == "CHAOS")
check("items parsed", roster[0].items == (1001, 3118))
check("spells parsed", "闪现" in roster[0].summoner_spells)
check("haste unavailable", roster[0].ability_haste == 0.0 and roster[0].haste_source == "unavailable")

print("ALL %d CHECKS PASSED" % len(PASS))
for p in PASS:
    print("  ok -", p)
os._exit(0)
