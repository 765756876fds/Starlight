import json
from datetime import datetime

log_file = 'data/events/2026-09-15.jsonl'
events = []
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

print(f'总事件数: {len(events)}')
print()

# 按类型统计
from collections import Counter
type_counts = Counter(e['event_type'] for e in events)

print('=== 事件类型统计 ===')
for etype, count in sorted(type_counts.items()):
    print(f'  {etype}: {count}')

print()
print('=== game.started / game.ended 时间线 ===')
game_starts = [e for e in events if e['event_type'] == 'game.started']
game_ends = [e for e in events if e['event_type'] == 'game.ended']

print(f'game.started: {len(game_starts)} 次')
for i, e in enumerate(game_starts):
    ts = e['timestamp']
    dt = datetime.fromtimestamp(ts)
    print(f'  #{i+1} [{dt.strftime("%H:%M:%S")}] {e.get("payload", {})}')

print()
print(f'game.ended: {len(game_ends)} 次')
for i, e in enumerate(game_ends):
    ts = e['timestamp']
    dt = datetime.fromtimestamp(ts)
    print(f'  #{i+1} [{dt.strftime("%H:%M:%S")}] {e.get("payload", {})}')

print()
print('=== speech.started / speech.finished 统计 ===')
speech_started = [e for e in events if e['event_type'] == 'speech.started']
speech_finished = [e for e in events if e['event_type'] == 'speech.finished']

print(f'speech.started: {len(speech_started)} 次')
print(f'speech.finished: {len(speech_finished)} 次')

# 统计 finished 的 reason
reasons = Counter(e.get('payload', {}).get('reason', 'unknown') for e in speech_finished)
print(f'finished reasons: {dict(reasons)}')
