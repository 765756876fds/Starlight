# -*- coding: utf-8 -*-
"""TemporaryGameMemory 多记忆生命周期 + USER/SYSTEM 隔离测试。"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.lol.game_alarm import GameAlarmEngine, AlarmStatus


def fresh():
    eng = GameAlarmEngine({})
    eng._current_game_time = 1000.0
    return eng


fails = []
def chk(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond: fails.append(name)

# 1. 双记忆并存
eng = fresh()
a = eng.create_flash_alarm("莎弥拉")
b = eng.create_flash_alarm("阿卡丽")
chk("A!=B id", a.alarm_id != b.alarm_id)
um = eng.get_user_memories()
titles = sorted(x.title for x in um)
chk("list 含两条用户记忆", titles == ["莎弥拉 闪现", "阿卡丽 闪现"])

# 2. 加入 SYSTEM 龙提醒，不污染 list
sysal = eng.schedule_objective_reminder("小龙刷新", game_time_end=1400.0, lead_time=40.0)
um = eng.get_user_memories()
names = " ".join(x.title for x in um)
chk("list 不含 SYSTEM 龙", "龙" not in names and len(um) == 2)

# 3. remaining 选最近到期（莎弥拉/阿卡丽都是300s=1300）
rem = sorted(um, key=lambda x: x.game_time_end)[0]
chk("remaining 选 active 用户记忆", rem in (a, b))

# 4. cancel_last 取消最近创建的（阿卡丽），不动莎弥拉和 SYSTEM
c = eng.cancel_last_user_memory()
chk("cancel_last 取消阿卡丽", c is not None and "阿卡丽" in c.title)
chk("取消后阿卡丽 CANCELLED", eng.get_alarm_by_id(b.alarm_id).status == AlarmStatus.CANCELLED)
chk("莎弥拉仍 ACTIVE", eng.get_alarm_by_id(a.alarm_id).status == AlarmStatus.ACTIVE)
chk("SYSTEM 龙仍 ACTIVE", eng.get_alarm_by_id(sysal.alarm_id).status == AlarmStatus.ACTIVE)
chk("剩余用户记忆只剩莎弥拉", len(eng.get_user_memories()) == 1)

# 5. 再取消一次 -> 这次取消莎弥拉；第三次 -> None
c2 = eng.cancel_last_user_memory()
chk("第二次取消莎弥拉", c2 is not None and "莎弥拉" in c2.title)
chk("第三次无对象返回 None", eng.cancel_last_user_memory() is None)
chk("已无用户记忆", eng.get_user_memories() == [])

# 6. 到期绑定：新引擎，莎弥拉 duration=300, end=1300；推进到 1286（lead=15 触发）
eng = fresh()
sa = eng.create_flash_alarm("莎弥拉")
eng._on_game_time_update(type("E", (), {"get": staticmethod(lambda k, d=None: 1286.0)})())
chk("莎弥拉到期触发 TRIGGERED", eng.get_alarm_by_id(sa.alarm_id).status == AlarmStatus.TRIGGERED)

# 7. 已取消的不再触发
eng2 = fresh()
x = eng2.create_flash_alarm("锐雯")
eng2.cancel_alarm(x.alarm_id)
eng2._on_game_time_update(type("E", (), {"get": staticmethod(lambda k, d=None: 99999.0)})())
chk("已取消不触发", eng2.get_alarm_by_id(x.alarm_id).status == AlarmStatus.CANCELLED)

# 8. 空查询失败真话
eng3 = fresh()
chk("空查询 get_user_memories=[]", eng3.get_user_memories() == [])
chk("空查询 cancel=None", eng3.cancel_last_user_memory() is None)

print("----")
if fails:
    print("FAILURES:", fails); raise SystemExit(1)
print("ALL MEMORY LIFECYCLE TESTS PASS")
