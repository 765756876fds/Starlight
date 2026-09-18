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
    print(f'#{i+1} [时间戳: {ts:.0f}]')
    print(f'    payload: {json.dumps(payload, ensure_ascii=False)}')
    print()

# 找对应的 game.started 事件
print('=== game.started 事件 ===')
game_start_events = [e for e in events if e['event_type'] == 'game.started']
print(f'数量: {len(game_start_events)}')
for i, e in enumerate(game_start_events):
    ts = e['timestamp']
    payload = e.get('payload', {})
    print(f'  #{i+1} [{ts:.0f}] {payload}')
