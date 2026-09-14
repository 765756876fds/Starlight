"""审计测试：EventBus + StateDiff + GameAlarm + Intent 综合验证"""
import sys, os, time, json
sys.path.insert(0, '.')

from app.core.event_bus import get_event_bus, Event, EventType
from app.core.command_bus import get_command_bus, Command
from app.core.state_store import get_state_store
from app.lol.snapshot import GameSnapshot, PlayerInfo, SnapshotBuilder
from app.lol.state_diff import StateDiff
from app.lol.game_alarm import GameAlarmEngine, AlarmType
from app.voice.intent import RuleBasedIntent, Intent

print('=' * 60)
print('综合审计测试：EventBus + StateDiff + GameAlarm + Intent')
print('=' * 60)

# ===== C4. EventBus + StateDiff =====
print('\n--- C4. EventBus + StateDiff 测试 ---')

event_bus = get_event_bus()
received_events = []

def event_handler(event):
    received_events.append(event)

# 订阅关键事件
for et in [EventType.PLAYER_LEVEL_UP, EventType.CHAMPION_KILL,
           EventType.TOWER_DESTROYED, EventType.DRAGON_KILLED,
           EventType.BARON_KILLED, EventType.ITEM_CHANGED,
           EventType.PLAYER_SUMMONER_SPELL_USED, EventType.GAME_ENDED,
           EventType.ALARM_TRIGGERED, EventType.ALARM_CREATED]:
    event_bus.subscribe(et, event_handler)

# 构造旧 Snapshot
old_snapshot = GameSnapshot()
old_snapshot.game_time = 300.0
old_snapshot.is_game_running = True
old_p = PlayerInfo(
    summoner_name="TestPlayer",
    champion_name="Ahri",
    team="ORDER",
    is_current_player=True,
    level=6,
    kills=2, deaths=1, assists=3,
    items=[{"itemID": 1001}, {"itemID": 1002}],
    summoner_spells=[{"displayName": "Flash", "cooldownRemaining": 0},
                      {"displayName": "Ignite", "cooldownRemaining": 0}],
)
old_snapshot.players = [old_p]
old_snapshot.current_player = old_p
old_snapshot.ally_team = [old_p]

# 构造新 Snapshot（升级、击杀、物品变化、技能使用）
new_snapshot = GameSnapshot()
new_snapshot.game_time = 360.0
new_snapshot.is_game_running = True
new_p = PlayerInfo(
    summoner_name="TestPlayer",
    champion_name="Ahri",
    team="ORDER",
    is_current_player=True,
    level=7,  # 升级
    kills=3, deaths=1, assists=3,  # 多一个击杀
    items=[{"itemID": 1001}, {"itemID": 1003}],  # 物品变化
    summoner_spells=[{"displayName": "Flash", "cooldownRemaining": 280},  # 闪现用了
                      {"displayName": "Ignite", "cooldownRemaining": 0}],
)
new_snapshot.players = [new_p]
new_snapshot.current_player = new_p
new_snapshot.ally_team = [new_p]

# 添加事件数据（模拟 eventdata）
new_snapshot.recent_events = [
    {"EventName": "ChampionKill", "EventTime": 350, "KillerName": "TestPlayer", "VictimName": "Enemy1"},
    {"EventName": "DragonKill", "EventTime": 340, "DragonType": "fire", "KillerName": "TestPlayer"},
    {"EventName": "TurretKilled", "EventTime": 345, "TurretKilled": "Turret_T1_01", "KillerName": "TestPlayer"},
]

# StateDiff
state_diff = StateDiff()
state_diff._last_snapshot = old_snapshot
events = state_diff.diff(new_snapshot)

print(f'\nStateDiff 产生事件数: {len(events)}')
event_types_found = set()
for e in events:
    event_types_found.add(e.event_type)
    print(f'  - {e.event_type}: {e.data}')

# 发布事件到 EventBus
for e in events:
    event_bus.publish(Event(event_type=e.event_type, data=e.data, source="test"))

time.sleep(0.2)
print(f'\nEventBus 收到事件数: {len(received_events)}')
bus_event_types = set(e.event_type for e in received_events)
print(f'EventBus 事件类型: {bus_event_types}')

# 验证关键事件
expected_events = [EventType.PLAYER_LEVEL_UP, EventType.CHAMPION_KILL,
                   EventType.DRAGON_KILLED, EventType.TOWER_DESTROYED,
                   EventType.ITEM_CHANGED, EventType.PLAYER_SUMMONER_SPELL_USED]
for et in expected_events:
    status = '✅' if et in event_types_found else '❌'
    print(f'  {status} {et}')

# ===== C5. GameAlarm =====
print('\n--- C5. GameAlarm 测试 ---')

game_alarm = GameAlarmEngine({})
game_alarm.start()
game_alarm._current_game_time = 360.0

alarm_results = []
def alarm_handler(event):
    alarm_results.append(event)

event_bus.subscribe(EventType.ALARM_CREATED, alarm_handler)
event_bus.subscribe(EventType.ALARM_TRIGGERED, alarm_handler)

# 测试 1: 自定义计时器 "两分钟后提醒我"
print('\n测试1: 自定义计时器 (2分钟)')
alarm1 = game_alarm.create_custom_alarm("两分钟提醒", duration=120.0)
print(f'  Alarm ID: {alarm1.alarm_id}')
print(f'  类型: {alarm1.alarm_type}')
print(f'  开始时间: {alarm1.game_time_start}')
print(f'  结束时间: {alarm1.game_time_end}')
print(f'  持续时间: {alarm1.duration}')
print(f'  提前提醒: {alarm1.lead_time}')
print(f'  状态: {alarm1.status}')

# 测试 2: 闪现 "对面莎弥拉闪现了"
print('\n测试2: 闪现计时 (莎弥拉)')
alarm2 = game_alarm.create_flash_alarm("莎弥拉")
print(f'  标题: {alarm2.title}')
print(f'  类型: {alarm2.alarm_type}')
print(f'  持续时间: {alarm2.duration}s (默认300s)')

# 测试 3: 技能 "莎弥拉 W 用了"
print('\n测试3: 技能计时 (莎弥拉 W)')
alarm3 = game_alarm.create_spell_alarm("莎弥拉", "W", cooldown=60.0)
print(f'  标题: {alarm3.title}')
print(f'  持续时间: {alarm3.duration}s')

# 测试 4: 眼位 "这里插了个眼"
print('\n测试4: 眼位计时')
alarm4 = game_alarm.create_ward_alarm("当前位置")
print(f'  标题: {alarm4.title}')
print(f'  持续时间: {alarm4.duration}s (默认120s)')

# 测试 5: 触发提醒（模拟时间推进）
print('\n测试5: 触发提醒验证')
active_alarms = game_alarm.get_active_alarms()
print(f'  活跃提醒数: {len(active_alarms)}')
for a in active_alarms:
    print(f'    - {a.title} (剩余 {a.remaining(360.0):.0f}s)')

# 模拟时间推进到自定义计时器快结束
print('\n  模拟时间推进到 470s (自定义计时器剩余10s)')
game_alarm._current_game_time = 470.0
game_alarm._check_alarms(470.0)
time.sleep(0.2)
triggered = [e for e in alarm_results if e.event_type == EventType.ALARM_TRIGGERED]
print(f'  触发的提醒数: {len(triggered)}')
for t in triggered:
    print(f'    - {t.data.get("alarm", {}).get("title", "?")}')

# 验证是否只提醒一次
print('\n  再次检查（应不再触发）')
alarm_results.clear()
game_alarm._check_alarms(471.0)
time.sleep(0.1)
triggered2 = [e for e in alarm_results if e.event_type == EventType.ALARM_TRIGGERED]
print(f'  再次触发数: {len(triggered2)} (应为0)')

# ===== C6. Intent 规则测试 =====
print('\n--- C6. Intent 规则匹配测试 ---')

test_cases = [
    ("下一首", "music.next"),
    ("暂停", "music.pause"),
    ("继续播放", "music.resume"),
    ("上一首", "music.prev"),
    ("音量大一点", "music.volume_up"),
    ("音量小一点", "music.volume_down"),
    ("两分钟后提醒我", "alarm.custom"),
    ("对面莎弥拉闪现了", "alarm.flash"),
    ("莎弥拉 W用了", "alarm.spell"),
    ("这里插了个眼", "alarm.ward"),
    ("播放周杰伦的歌", "music.search_play"),
    ("跳高潮", "music.highlight"),
]

passed = 0
failed = 0
for text, expected in test_cases:
    intent = RuleBasedIntent.match(text)
    actual = intent.intent_type if intent else "None"
    status = '✅' if actual == expected else '❌'
    if actual == expected:
        passed += 1
    else:
        failed += 1
    print(f'  {status} "{text}" -> {actual} (期望 {expected})')
    if intent and intent.params:
        print(f'       params: {intent.params}')

print(f'\n  规则匹配结果: {passed}/{len(test_cases)} 通过, {failed} 失败')

# ===== 总结 =====
print('\n' + '=' * 60)
print('综合测试总结')
print('=' * 60)
print(f'StateDiff 事件产生: {len(events)} 个')
print(f'EventBus 事件接收: {len(received_events)} 个')
print(f'GameAlarm 创建: 4 个 (自定义/闪现/技能/眼位)')
print(f'GameAlarm 触发验证: {"通过" if len(triggered) > 0 else "失败"}')
print(f'Intent 规则匹配: {passed}/{len(test_cases)} 通过')
print('=' * 60)
