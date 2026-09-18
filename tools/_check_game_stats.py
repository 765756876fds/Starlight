import json
from datetime import datetime

log_file = 'data/events/2026-09-15.jsonl'
events = []
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

# GAME-REAL-07 的两局游戏：
# Game 3: started 22:05:58, ended 22:20:53
# Game 4: started 22:25:10, ended ?

game3_start = 1789481158.979211  # 22:05:58
game3_end = 1789482053.0610123   # 22:20:53
game4_start = 1789482310.8580866 # 22:25:10

# 按游戏分段
game3_events = [e for e in events if game3_start <= e['timestamp'] <= game3_end]
game4_events = [e for e in events if e['timestamp'] >= game4_start]

print('=== Game 3 (22:05:58 - 22:20:53) ===')
print(f'总事件数: {len(game3_events)}')

from collections import Counter
type_counts_3 = Counter(e['event_type'] for e in game3_events)
for etype, count in sorted(type_counts_3.items()):
    print(f'  {etype}: {count}')

print()
print('=== Game 4 (22:25:10 - 现在) ===')
print(f'总事件数: {len(game4_events)}')

type_counts_4 = Counter(e['event_type'] for e in game4_events)
for etype, count in sorted(type_counts_4.items()):
    print(f'  {etype}: {count}')

print()
print('=== Game 3 Speech 统计 ===')
game3_speech_started = [e for e in game3_events if e['event_type'] == 'speech.started']
game3_speech_finished = [e for e in game3_events if e['event_type'] == 'speech.finished']
print(f'speech.started: {len(game3_speech_started)}')
print(f'speech.finished: {len(game3_speech_finished)}')
reasons_3 = Counter(e.get('payload', {}).get('reason', 'unknown') for e in game3_speech_finished)
print(f'finished reasons: {dict(reasons_3)}')

print()
print('=== Game 4 Speech 统计 ===')
game4_speech_started = [e for e in game4_events if e['event_type'] == 'speech.started']
game4_speech_finished = [e for e in game4_events if e['event_type'] == 'speech.finished']
print(f'speech.started: {len(game4_speech_started)}')
print(f'speech.finished: {len(game4_speech_finished)}')
reasons_4 = Counter(e.get('payload', {}).get('reason', 'unknown') for e in game4_speech_finished)
print(f'finished reasons: {dict(reasons_4)}')

print()
print('=== 多击杀 vs ChampionKill 比例检查 ===')
print(f'Game 3: ChampionKill={type_counts_3.get("game.champion_kill", 0)}, MultiKill={type_counts_3.get("game.multi_kill", 0)}')
print(f'Game 4: ChampionKill={type_counts_4.get("game.champion_kill", 0)}, MultiKill={type_counts_4.get("game.multi_kill", 0)}')
