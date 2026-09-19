"""Verify cooldown reminders follow game clock, not wall clock."""
import sys, time
sys.path.insert(0, ".")
from app.lol.cooldown.manager import CooldownManager

spoken = []
mgr = CooldownManager(speech_enqueue=lambda t, eid: spoken.append(t))

# 1) 锚定游戏时钟：当前游戏时间 300
mgr.set_game_time(300.0)
r = mgr.start_reminder("石头人", "W", side="enemy")
assert r is not None and r.game_clock, "should use game clock"
# W est ~14s, remind_before=5 -> remind at game 309
print("used_at=", r.used_at, "remind_at=", r.remind_at, "ready_at=", r.ready_at)

# 2) 游戏时间只走到 305 -> 还不该提醒（即使现实过了好几秒）
spoken.clear()
time.sleep(0.5)
mgr.set_game_time(305.0)
assert spoken == [], f"should not remind yet, got {spoken}"

# 3) 游戏时间走到 309 -> 应提醒
mgr.set_game_time(309.0)
assert len(spoken) == 1, f"expected remind at game 309, got {spoken}"
print("remind text:", spoken[0])
assert "大约5秒好" in spoken[0], spoken[0]

# 4) 到 ready (314) -> ready event, reminder cleared
evts = []
mgr._on_event = lambda c, d: evts.append(c)
mgr.set_game_time(314.0)
assert mgr.get(r.id) is None, "should be removed at ready"
print("game-clock reminder lifecycle OK")

# 5) reset_clock clears on game end
mgr.set_game_time(400.0)
mgr.start_reminder("石头人", "R")
assert len(mgr.list_active()) == 1
mgr.reset_clock()
assert len(mgr.list_active()) == 0 and mgr._game_time < 0
print("reset_clock OK")

print("ALL GAME-CLOCK OK")
