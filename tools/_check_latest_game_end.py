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
    print(f'    timestamp: {ts}')
    print(f'    payload: {json.dumps(payload, ensure_ascii=False)}')
    print()

# 找最新的 game.ended
print('=== 最新的 game.ended ===')
latest_end = game_end_events[-1]
print(f'    timestamp: {latest_end["timestamp"]}')
print(f'    payload: {json.dumps(latest_end.get("payload", {}), ensure_ascii=False, indent=4)}')
