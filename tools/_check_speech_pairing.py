import json
from datetime import datetime

log_file = 'data/events/2026-09-15.jsonl'
events = []
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

# 找最新的 2 局游戏（GAME-REAL-07 测试的）
# game.started 时间点
game_starts = [e for e in events if e['event_type'] == 'game.started']
print('=== 所有 game.started ===')
for i, e in enumerate(game_starts):
    ts = e['timestamp']
    dt = datetime.fromtimestamp(ts)
    print(f'  #{i+1} [{dt.strftime("%H:%M:%S")}]')

print()
print('=== 所有 game.ended ===')
game_ends = [e for e in events if e['event_type'] == 'game.ended']
for i, e in enumerate(game_ends):
    ts = e['timestamp']
    dt = datetime.fromtimestamp(ts)
    payload = e.get('payload', {})
    result = payload.get('result', 'None')
    print(f'  #{i+1} [{dt.strftime("%H:%M:%S")}] result={result}')

# 找 GAME-REAL-07 的两局游戏（最新的两局）
# 最新的两个 game.started 是 #3 (22:05:58) 和 #4 (22:25:10)
# 对应的 game.ended 是 #6 (22:20:53) 和 ...（还没结束？）

print()
print('=== Speech 配对检查（最新 20 个 speech 事件）===')
speech_events = [e for e in events if e['event_type'].startswith('speech.')]
print(f'总 speech 事件数: {len(speech_events)}')

# 显示最新 20 个
for e in speech_events[-20:]:
    ts = e['timestamp']
    dt = datetime.fromtimestamp(ts)
    etype = e['event_type']
    payload = e.get('payload', {})
    reason = payload.get('reason', '')
    text = payload.get('text', '')[:30] if payload.get('text') else ''
    print(f'  [{dt.strftime("%H:%M:%S")}] {etype} reason={reason} text={text}...')

print()
print('=== 未配对的 started 检查 ===')
# 按顺序匹配 started 和 finished
started_count = 0
unpaired = []
for e in speech_events:
    if e['event_type'] == 'speech.started':
        started_count += 1
        unpaired.append(e)
    elif e['event_type'] == 'speech.finished':
        if unpaired:
            unpaired.pop(0)  # 配对成功
        else:
            print(f'  警告: 有 finished 但没有对应的 started')

print(f'未配对的 started 数量: {len(unpaired)}')
for e in unpaired:
    ts = e['timestamp']
    dt = datetime.fromtimestamp(ts)
    payload = e.get('payload', {})
    text = payload.get('text', '')[:30] if payload.get('text') else ''
    print(f'  [{dt.strftime("%H:%M:%S")}] started: {text}...')
