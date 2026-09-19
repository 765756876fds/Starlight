# -*- coding: utf-8 -*-
"""Agent 重启附着对局时 Objective 首次刷新排程时间基准回归。"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.lol.game_alarm import GameAlarmEngine, AlarmStatus
from app.lol.objective_timer import ObjectiveTimer
from app.core.event_bus import Event, EventType


def make(gt):
    ga = GameAlarmEngine({})
    ot = ObjectiveTimer(ga)
    ot.start()
    ev = Event(event_type=EventType.GAME_STARTED, data={"game_time": gt, "game_mode": "practice"})
    return ga, ot, ev


def created_objectives(ga):
    return {a.data.get("objective") for a in ga.get_active_alarms()}


fails = []
def chk(n, c):
    print(("PASS" if c else "FAIL"), n)
    if not c: fails.append(n)


# Test 1：晚期重启 1860s —— 首次刷新不应排程
ga, ot, ev = make(1860.0)
ot._on_game_start(ev)
objs = created_objectives(ga)
chk("game_alarm 时钟同步=1860", ga._current_game_time == 1860.0)
chk("晚期不排巢虫", "void_grubs" not in objs)
chk("晚期不排小龙", "dragon" not in objs)
chk("晚期不排大龙", "baron" not in objs)

# Test 2：早期重启 100s —— 仍可正常排首次刷新
ga, ot, ev = make(100.0)
ot._on_game_start(ev)
objs = created_objectives(ga)
chk("早期时钟=100", ga._current_game_time == 100.0)
chk("早期排巢虫(480-60=420>100)", "void_grubs" in objs)
chk("早期排小龙(300-40=260>100)", "dragon" in objs)

# Test 3：边界 first_spawn-lead-1 应创建；first_spawn-lead 应跳过
# 巢虫 first_spawn=480 lead=60 -> 触发线 420
ga, ot, ev = make(419.0)   # 419 < 420 -> 应创建
ot._on_game_start(ev)
chk("边界 419 排巢虫", "void_grubs" in created_objectives(ga))

ga, ot, ev = make(420.0)   # 420 == 420 -> guard 跳过
ot._on_game_start(ev)
chk("边界 420 不排巢虫", "void_grubs" not in created_objectives(ga))

print("----")
if fails:
    print("FAILURES:", fails); raise SystemExit(1)
print("ALL RESTART OBJECTIVE TIMEBASE TESTS PASS")
