# -*- coding: utf-8 -*-
"""临时记忆 / 快速反馈 链路测试（脚本式，直接 python 运行）"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 1) Intent 规则：新查询/取消意图
from app.voice.intent import RuleBasedIntent
for text, expect in [
    ("刚才记了什么", "alarm.list"),
    ("我记了哪些", "alarm.list"),
    ("还有多久", "alarm.remaining"),
    ("取消刚才那个", "alarm.cancel_last"),
    ("取消提醒", "alarm.cancel_last"),
]:
    it = RuleBasedIntent.match(text)
    assert it.intent_type == expect, f"{text} -> {it.intent_type}, expect {expect}"
print("1 intent rules OK")

# 闪现/自定义计时规则仍正常
it = RuleBasedIntent.match("对面莎弥拉闪现了")
assert it.intent_type == "alarm.flash" and it.params.get("champion"), it
it = RuleBasedIntent.match("3分30秒后提醒我")
assert it.intent_type == "alarm.custom" and it.params.get("duration"), it
print("2 flash/custom intent OK, dur=", it.params.get("duration"))

# 2) GameAlarm 作为 TemporaryGameMemory：创建/查询/剩余/取消
from app.lol.game_alarm import GameAlarmEngine, AlarmStatus
eng = GameAlarmEngine({})
a = eng.create_flash_alarm("莎弥拉", game_time=100.0)
assert len(eng.get_active_alarms()) == 1
eng._current_game_time = 110.0
active = eng.get_active_alarms()
assert active and active[0].title == "莎弥拉 闪现"
rem = active[0].remaining(eng._current_game_time)
assert 280 <= rem <= 290, rem  # 300 - 10
print("3 memory create/query/remaining OK, rem=", rem)

# 取消
eng.cancel_alarm(a.alarm_id)
assert eng.get_alarm_by_id(a.alarm_id).status == AlarmStatus.CANCELLED
assert len(eng.get_active_alarms()) == 0
print("4 cancel OK")

# 3) 重复请求不爆炸：连续创建多个
eng2 = GameAlarmEngine({})
for _ in range(3):
    eng2.create_flash_alarm("A", game_time=0)
assert len(eng2.get_active_alarms()) == 3
print("5 multi create OK")

# 4) SPEECH_SAY 快速反馈事件结构（验证 TTS 直连路径存在）
from app.core.event_bus import EventType, get_event_bus, Event
seen = []
eb = get_event_bus()
eb.subscribe(EventType.SPEECH_SAY, lambda e: seen.append(e.get("text")))
eb.publish(Event(event_type=EventType.SPEECH_SAY, data={"text": "记好了，测试"}, source="test"))
assert seen and seen[0] == "记好了，测试"
print("6 SPEECH_SAY feedback channel OK")

# 5) Snapshot runes 解析（缺失不崩，存在则保留）
from app.lol.snapshot import SnapshotBuilder
p = SnapshotBuilder._parse_player({
    "summonerName": "T", "team": "CHAOS", "championName": "Ahri",
    "items": [{"itemId": 1056}], "summonerSpells": {}, "runes": [{"id": 991, "name": "电刑"}],
}, "")
assert p.champion_name == "Ahri"
assert isinstance(p.items, list) and p.items[0]["itemId"] == 1056
assert isinstance(p.runes, list) and p.runes[0]["name"] == "电刑"
# 缺失 runes 不崩
p2 = SnapshotBuilder._parse_player({"summonerName": "T2", "team": "ORDER"}, "")
assert p2.runes == [] and p2.items == []
print("7 snapshot enemy items/runes/spells parse OK")

print("ALL memory/feedback tests OK")

