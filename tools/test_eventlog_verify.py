import sys, os, json, time
sys.path.insert(0, '.')

from app.core.event_bus import Event, EventType, get_event_bus
from app.core.event_log import EventLog

event_bus = get_event_bus()
event_log = EventLog(log_dir='./data/events')
event_log.start()

event_bus.publish(Event(
    event_type=EventType.SYSTEM_STARTUP,
    data={'timestamp': 1234567890},
    source='test'
))

event_bus.publish(Event(
    event_type='game.champion_kill',
    data={'killer': 'A', 'victim': 'B', 'kills': 5},
    source='lol'
))

event_bus.publish(Event(
    event_type='lcu.gameflow_changed',
    data={'old_phase': 'Lobby', 'new_phase': 'InProgress'},
    source='lol'
))

event_bus.publish(Event(
    event_type='music.next',
    data={'song': 'test_song'},
    source='music'
))

time.sleep(0.2)
event_log.stop()

date_str = time.strftime('%Y-%m-%d')
filepath = './data/events/' + date_str + '.jsonl'
print('File:', filepath)
print('Exists:', os.path.exists(filepath))
print()

with open(filepath, 'r', encoding='utf-8') as f:
    lines = f.readlines()
    print('Total lines:', len(lines))
    print()
    for i, line in enumerate(lines):
        entry = json.loads(line.strip())
        eid = entry['event_id'][:8]
        print('--- Event', i+1, '---')
        print('  event_id:', eid, '...')
        print('  event_type:', entry['event_type'])
        print('  timestamp:', entry['timestamp'])
        print('  source:', entry['source'])
        print('  payload:', entry['payload'])
        print()
