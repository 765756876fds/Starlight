import json
from collections import Counter

log_file = 'data/events/2026-09-15.jsonl'
events = []
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

print(f'总事件数: {len(events)}')
print()

# 统计事件类型
type_counts = Counter(e['event_type'] for e in events)
print('事件类型统计:')
for etype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
    print(f'  {etype}: {count}')

print()
print('最近 15 个事件:')
for e in events[-15:]:
    ts = e['timestamp']
    etype = e['event_type']
    print(f'  [{ts:.0f}] {etype}')
