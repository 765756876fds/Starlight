"""Cooldown MUST be driven solely by in-game gameTime. Covers 7 scenarios."""
import sys, os, time
sys.path.insert(0, ".")
from app.lol.cooldown.manager import CooldownManager

spoken = []
mgr = CooldownManager(speech_enqueue=lambda t, eid: spoken.append((t, eid)))

# Test 1: unsynced -> refuse, no timer, no wall-clock
r = mgr.start_reminder("石头人", "W", side="enemy")
assert r is None, "must refuse before sync"
assert mgr.list_active() == [], "no timer when unsynced"
assert any("尚未同步" in t for t, _ in spoken), spoken
print("T1 unsynced refuse OK")

# Test 2: gameTime=0 sync
mgr.set_game_time(0.0)
assert mgr._clock_synced and mgr._game_time == 0.0
spoken.clear()
r0 = mgr.start_reminder("石头人", "W")
assert r0 is not None and r0.game_clock and r0.used_at == 0.0, r0
print("T2 gameTime=0 accepted, anchored to game clock (cd=%.1f)" % r0.estimated_cooldown)

# cast at game 300
mgr.set_game_time(300.0)
spoken.clear()
r = mgr.start_reminder("石头人", "W")
cast = r.used_at
remind = r.remind_at
ready = r.ready_at
assert cast == 300.0
print("T3 started at %.0f, remind=%.0f ready=%.0f" % (cast, remind, ready))

# Test 3: just before remind -> no
mgr.set_game_time(remind - 1.0)
assert spoken == [], spoken
# Test 4: pause — wall time passes but gameTime frozen
time.sleep(1.0)
mgr.set_game_time(remind - 1.0)
assert spoken == [], spoken
print("T4 pause (gameTime frozen) no remind OK")

# Test 5: reconnect jumps back after 30s wall, still below remind
mgr.set_game_time(cast + 2.0)
assert spoken == [], spoken
print("T5 reconnect gameTime=%.0f no remind OK" % (cast + 2.0))

# reach remind -> speak
mgr.set_game_time(remind)
assert len(spoken) == 1 and "大约" in spoken[0][0] and "秒好" in spoken[0][0], spoken
print("T3/T5 remind at game %.0f: %s" % (remind, spoken[0][0]))

# reach ready -> removed
mgr.set_game_time(ready)
assert mgr.get(r.id) is None
print("ready at %.0f, cleared" % ready)

# Test 6: GAME_ENDED
spoken.clear()
mgr.set_game_time(300.0)
mgr.start_reminder("石头人", "R")
assert len(mgr.list_active()) == 1
mgr.reset_clock()
assert len(mgr.list_active()) == 0
assert mgr._clock_synced is False and mgr._game_time == 0.0
mgr.set_game_time(320.0)
assert len(mgr.list_active()) == 0
print("T6 GAME_ENDED clears + unsync OK")

# Test 7: new game resync
mgr.set_game_time(0.0)
r2 = mgr.start_reminder("石头人", "W")
assert r2 is not None and r2.used_at == 0.0
print("T7 new game resync OK")

print("ALL GAME-CLOCK TESTS OK")
os._exit(0)
