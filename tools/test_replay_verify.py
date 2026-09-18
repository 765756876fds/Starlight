import sys, os, json, time
sys.path.insert(0, '.')

from app.core.event_bus import Event, get_event_bus
from app.core.event_replay import EventReplay

# 重置 EventBus 单例
import app.core.event_bus as eb_module
eb_module._event_bus = None

event_bus = get_event_bus()
events_received = []

def on_event(event):
    events_received.append(event)
    print("  Replayed:", event.event_type, "|", event.source)

event_bus.subscribe_all(on_event)

# Replay 真实文件
date_str = time.strftime('%Y-%m-%d')
filepath = './data/events/' + date_str + '.jsonl'
print('Replaying:', filepath)
print()

replay = EventReplay(event_bus)
count = replay.replay_file(filepath)

print()
print('=' * 50)
print('Replay result:')
print('  Total replayed:', count)
print('  Total received:', len(events_received))
print('  Skip count:', replay.get_skip_count())
print()

# 检查顺序是否正确
if len(events_received) >= 2:
    print('Order check:')
    for i, e in enumerate(events_received):
        print(f'  [{i+1}] {e.event_type}')
