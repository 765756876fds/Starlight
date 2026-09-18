import json

log_file = 'data/events/2026-09-15.jsonl'
events = []
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

# 找所有 game.started 和 game.ended
game_start_events = [e for e in events if e['event_type'] == 'game.started']
game_end_events = [e for e in events if e['event_type'] == 'game.ended']

print(f'game.started 数量: {len(game_start_events)}')
print(f'game.ended 数量: {len(game_end_events)}')

print()
print('=== 所有 game.started 事件 ===')
for i, e in enumerate(game_start_events):
    ts = e['timestamp']
    payload = e.get('payload', {})
    print(f'#{i+1} [{ts:.0f}] game.started')
    print(f'     payload: {payload}')

print()
print('=== 所有 game.ended 事件 ===')
for i, e in enumerate(game_end_events):
    ts = e['timestamp']
    payload = e.get('payload', {})
    print(f'#{i+1} [{ts:.0f}] game.ended')
    print(f'     payload: {payload}')

print()
print('=== 第一局游戏结束前 30 秒的事件 ===')
if len(game_end_events) >= 1:
    end_ts = game_end_events[0]['timestamp']
    # 找结束前 30 秒的事件
    before_end = [e for e in events if end_ts - 30 < e['timestamp'] < end_ts + 5]
    print(f'结束时间: {end_ts:.0f}')
    print(f'结束前 30 秒到结束后 5 秒的事件数: {len(before_end)}')
    for e in before_end[-20:]:
        ts = e['timestamp']
        etype = e['event_type']
        print(f'  [{ts:.0f}] {etype}')

print()
print('=== 第二局游戏结束前 30 秒的事件 ===')
if len(game_end_events) >= 2:
    end_ts = game_end_events[1]['timestamp']
    before_end = [e for e in events if end_ts - 30 < e['timestamp'] < end_ts + 5]
    print(f'结束时间: {end_ts:.0f}')
    print(f'结束前 30 秒到结束后 5 秒的事件数: {len(before_end)}')
    for e in before_end[-20:]:
        ts = e['timestamp']
        etype = e['event_type']
        print(f'  [{ts:.0f}] {etype}')
