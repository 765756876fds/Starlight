# -*- coding: utf-8 -*-
"""审计：SYSTEM alarm 与 USER_MEMORY 分离"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.lol.game_alarm import GameAlarmEngine, AlarmStatus, AlarmType

eng = GameAlarmEngine({})
eng._current_game_time = 100.0

# 系统龙提醒（更近，剩余 60s）
sys_a = eng.schedule_objective_reminder("龙刷新", game_time_end=160.0, lead_time=40.0)
assert sys_a.data.get("source") == "SYSTEM", sys_a.data
# 用户记忆（3 分钟后）
u = eng.create_flash_alarm("莎弥拉", game_time=100.0)
assert u.data.get("source") == "USER_MEMORY", u.data

# alarm.list 只返回用户记忆
users = eng.get_user_memories()
assert len(users) == 1 and users[0].alarm_id == u.alarm_id, users
print("1 list 只返回用户记忆 OK（系统龙提醒被排除）")

# remaining 针对用户记忆，不被更近的系统龙干扰
from app.lol.game_alarm import Alarm
def user_remaining():
    a = sorted(eng.get_user_memories(), key=lambda x: x.game_time_end)[0]
    return a.remaining(eng._current_game_time)
r = user_remaining()
assert 280 <= r <= 300, r  # 300-10，不是 60
print("2 remaining 针对用户记忆 OK, rem=", r)

# cancel_last 取消用户记忆，不动系统提醒
cancelled = eng.cancel_last_user_memory()
assert cancelled.alarm_id == u.alarm_id
assert eng.get_alarm_by_id(u.alarm_id).status == AlarmStatus.CANCELLED
assert eng.get_alarm_by_id(sys_a.alarm_id).status == AlarmStatus.ACTIVE
print("3 cancel_last 只取消用户记忆，系统龙提醒仍 ACTIVE OK")

# 取消后不会再触发
eng._check_alarms(eng._current_game_time)
assert eng.get_user_memories() == []
print("4 取消后不再出现在 active 用户记忆 OK")

# 没有用户记忆时 cancel 返回 None
assert eng.cancel_last_user_memory() is None
print("5 无用户记忆时取消返回 None OK")

# 单个玩家 runes 异常不拖死整个 snapshot
from app.lol.snapshot import SnapshotBuilder
import json
bad = {"summonerName": "A", "team": "CHAOS", "championName": "X",
       "items": "NOT_A_LIST", "runes": None, "summonerSpells": None}
p = SnapshotBuilder._parse_player(bad, "")
assert p.runes == []
# 不崩溃即可（items 原样保留，不强转）
print("6 runes/summonerSpells 异常值容错 OK")

# 正常 items/runes/spells 同时进入 PlayerInfo
good = {"summonerName": "B", "team": "ORDER", "championName": "Y",
        "items": [{"itemId": 1}], "runes": [{"id": 991}],
        "summonerSpells": {"summonerSpellOne": {"id": 4}, "summonerSpellTwo": {"id": 14}}}
p2 = SnapshotBuilder._parse_player(good, "")
assert p2.items and p2.runes and len(p2.summoner_spells) == 2
print("7 items/runes/spells 同时解析 OK")

print("ALL audit tests OK")
