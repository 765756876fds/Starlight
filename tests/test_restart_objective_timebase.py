# -*- coding: utf-8 -*-
"""Agent 重启附着对局时 Objective 首次刷新排程时间基准回归。"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.lol.game_alarm import GameAlarmEngine
from app.lol.objective_timer import ObjectiveTimer
from app.core.event_bus import Event, EventType


def created_objectives(ga):
    return {a.data.get("objective") for a in ga.get_active_alarms()}


fails = []
def chk(n, c):
    print(("PASS" if c else "FAIL"), n)
    if not c: fails.append(n)


# Test 1：晚期重启，且首帧 game_time=0（连接未就绪）-> 等首个真实 time_update
ga = GameAlarmEngine({})
ot = ObjectiveTimer(ga); ot.start()
ot._on_game_start(Event(event_type=EventType.GAME_STARTED,
                        data={"game_time": 0.0, "game_mode": "practice"}))
chk("首帧0不排巢虫", "void_grubs" not in created_objectives(ga))
chk("首帧0不排小龙", "dragon" not in created_objectives(ga))
# 首个真实 game_time=1860
ot._on_time_update(Event(event_type=EventType.GAME_TIME_UPDATE, data={"game_time": 1860.0}))
chk("time_update 同步=1860", ga._current_game_time == 1860.0)
chk("1860不排巢虫", "void_grubs" not in created_objectives(ga))
chk("1860不排小龙", "dragon" not in created_objectives(ga))
chk("1860不排大龙", "baron" not in created_objectives(ga))

# Test 2：晚期重启且首帧就带真实 game_time=1860 -> 直接不排
ga = GameAlarmEngine({})
ot = ObjectiveTimer(ga); ot.start()
ot._on_game_start(Event(event_type=EventType.GAME_STARTED, data={"game_time": 1860.0}))
chk("直接1860不排巢虫", "void_grubs" not in created_objectives(ga))

# Test 3：早期新局 game_time=100 -> 正常排
ga = GameAlarmEngine({})
ot = ObjectiveTimer(ga); ot.start()
ot._on_game_start(Event(event_type=EventType.GAME_STARTED, data={"game_time": 100.0}))
chk("早期排巢虫", "void_grubs" in created_objectives(ga))
chk("早期排小龙", "dragon" in created_objectives(ga))

# Test 4：首帧0 -> 早期 time_update=100 -> 正常排
ga = GameAlarmEngine({})
ot = ObjectiveTimer(ga); ot.start()
ot._on_game_start(Event(event_type=EventType.GAME_STARTED, data={"game_time": 0.0}))
ot._on_time_update(Event(event_type=EventType.GAME_TIME_UPDATE, data={"game_time": 100.0}))
chk("延迟到100排巢虫", "void_grubs" in created_objectives(ga))

# Test 5：边界 419 排 / 420 不排
ga = GameAlarmEngine({}); ot = ObjectiveTimer(ga); ot.start()
ot._on_game_start(Event(event_type=EventType.GAME_STARTED, data={"game_time": 419.0}))
chk("边界419排", "void_grubs" in created_objectives(ga))
ga = GameAlarmEngine({}); ot = ObjectiveTimer(ga); ot.start()
ot._on_game_start(Event(event_type=EventType.GAME_STARTED, data={"game_time": 420.0}))
chk("边界420不排", "void_grubs" not in created_objectives(ga))

print("----")
if fails:
    print("FAILURES:", fails); raise SystemExit(1)
print("ALL RESTART OBJECTIVE TIMEBASE TESTS PASS")
