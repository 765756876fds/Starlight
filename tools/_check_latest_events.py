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

# 最新 10 个事件
print('=== 最新 15 个事件 ===')
for e in events[-15:]:
    ts = e['timestamp']
    etype = e['event_type']
    # 转换为可读时间
    dt = datetime.fromtimestamp(ts)
    time_str = dt.strftime('%H:%M:%S')
    print(f'  [{time_str}] {etype}')

print()
print('=== 最新事件时间 ===')
latest_ts = events[-1]['timestamp']
dt = datetime.fromtimestamp(latest_ts)
print(f'最新事件时间: {dt.strftime("%Y-%m-%d %H:%M:%S")}')

print()
print('=== 当前时间 ===')
from datetime import datetime
now = datetime.now()
print(f'当前时间: {now.strftime("%Y-%m-%d %H:%M:%S")}')
