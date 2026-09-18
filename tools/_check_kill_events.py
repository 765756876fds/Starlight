import json
from collections import Counter

log_file = 'data/events/2026-09-15.jsonl'
events = []
with open(log_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            events.append(json.loads(line))

# 找所有击杀相关事件
kill_events = [e for e in events if 'kill' in e['event_type'] or 'ace' in e['event_type']]

print('=== 击杀事件时间线 ===')
for e in kill_events[-20:]:
    ts = e['timestamp']
    etype = e['event_type']
    payload = e.get('payload', {})
    killer = payload.get('killer_name', '')
    streak = payload.get('kill_streak', '')
    print(f'  [{ts:.0f}] {etype:25s} killer={killer:20s} streak={streak}')

print()
print('=== 关键统计 ===')
champion_kills = [e for e in events if e['event_type'] == 'game.champion_kill']
multikills = [e for e in events if e['event_type'] == 'game.multi_kill']
aces = [e for e in events if e['event_type'] == 'game.ace']

print(f'ChampionKill: {len(champion_kills)}')
print(f'Multikill: {len(multikills)}')
print(f'Ace: {len(aces)}')
print(f'ChampionKill/Multikill 比例: {len(champion_kills)}/{len(multikills)} = {len(champion_kills)/max(1,len(multikills)):.1f}:1')

print()
print('=== TTS 事件 ===')
speech_started = [e for e in events if e['event_type'] == 'speech.started']
speech_finished = [e for e in events if e['event_type'] == 'speech.finished']
print(f'speech.started: {len(speech_started)}')
print(f'speech.finished: {len(speech_finished)}')

print()
print('=== 音乐事件 ===')
music_events = [e for e in events if e['event_type'].startswith('music.')]
for e in music_events:
    print(f'  {e["event_type"]}: {e.get("payload", {})}')

print()
print('=== 游戏结束事件 ===')
game_end = [e for e in events if e['event_type'] == 'game.ended']
for e in game_end:
    print(f'  payload: {e.get("payload", {})}')
