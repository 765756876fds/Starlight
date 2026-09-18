import json
from collections import Counter

log_file = 'data/events/2026-09-15.jsonl'
events = []
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

# 检查 time_update 是否进入语音链路
time_update_events = [e for e in events if e['event_type'] == 'game.time_update']
print(f'game.time_update 数量: {len(time_update_events)}')

# 检查 speech 事件中有没有 time_update 相关的
speech_events = [e for e in events if e['event_type'].startswith('speech.')]
print(f'speech 事件数量: {len(speech_events)}')

# 检查 LCU 事件
lcu_events = [e for e in events if e['event_type'].startswith('lcu.')]
print()
print('LCU 事件:')
lcu_type_counts = Counter(e['event_type'] for e in lcu_events)
for etype, count in lcu_type_counts.items():
    print(f'  {etype}: {count}')

# 检查 system 事件
system_events = [e for e in events if e['event_type'].startswith('system.')]
print()
print('System 事件:')
for e in system_events:
    print(f'  {e["event_type"]}: {e.get("payload", {})}')

# 检查 music 事件
music_events = [e for e in events if e['event_type'].startswith('music.')]
print()
print('Music 事件:')
for e in music_events:
    print(f'  {e["event_type"]}: {e.get("payload", {})}')

# 检查 alarm 事件
alarm_events = [e for e in events if e['event_type'].startswith('alarm.')]
print()
print('Alarm 事件:')
for e in alarm_events:
    print(f'  {e["event_type"]}: {e.get("payload", {})}')
