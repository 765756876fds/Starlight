import json

log_file = 'data/events/2026-09-15.jsonl'
events = []
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

# 找所有 game.ended 事件
game_end_events = [e for e in events if e['event_type'] == 'game.ended']

print(f'=== game.ended 事件总数: {len(game_end_events)} ===')
print()

for i, e in enumerate(game_end_events):
    ts = e['timestamp']
    payload = e.get('payload', {})
    print(f'#{i+1}')
    print(f'    时间戳: {ts}')
    print(f'    payload: {json.dumps(payload, ensure_ascii=False, indent=4)}')
    print()

# 检查最新 game.ended 前后的事件
print('=== 最新 game.ended 前后 10 个事件 ===')
last_end_idx = None
for i, e in enumerate(events):
    if e['event_type'] == 'game.ended':
        last_end_idx = i

if last_end_idx:
    start = max(0, last_end_idx - 10)
    end = min(len(events), last_end_idx + 5)
    for i in range(start, end):
        e = events[i]
        ts = e['timestamp']
        etype = e['event_type']
        marker = " <-- GAME.ENDED" if etype == 'game.ended' else ""
        print(f'  [{i}] {etype}{marker}')
