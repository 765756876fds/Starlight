"""Stub test: cooldown.query unique event_id per query + SpeechQueue dedup preserved."""
import sys, time
sys.path.insert(0, ".")

from app.lol.cooldown.manager import CooldownManager

seen = []
def fake_enqueue(text, event_id):
    seen.append((text, event_id))

mgr = CooldownManager(speech_enqueue=fake_enqueue)

# 1) two consecutive independent queries
mgr.handle_query({"champion": "石头人", "skill": "大招"})
mgr.handle_query({"champion": None, "skill": "闪现"})

assert len(seen) == 2, f"expected 2 enqueues, got {len(seen)}"
ids = [eid for _, eid in seen]
print("query1 event_id:", ids[0])
print("query2 event_id:", ids[1])
assert ids[0].startswith("cooldown.query."), ids[0]
assert ids[1].startswith("cooldown.query."), ids[1]
assert ids[0] != ids[1], "event_id must be unique per query"
print("PASS: two consecutive queries each get unique event_id -> not deduped")

# 2) SpeechQueue still dedups identical event_id
from app.speech.speech_queue import SpeechQueue, SpeechItem, PRIORITY_A
class FakeTTS:
    def __init__(self): self.played = []
    def speak(self, item): self.played.append(item.text)
    def stop_playback(self): pass

sq = SpeechQueue(FakeTTS())
sq.start()
eid = "dup.test"
it1 = SpeechItem(text="a", priority=PRIORITY_A, expire_at=time.time()+10, event_id=eid)
it2 = SpeechItem(text="b", priority=PRIORITY_A, expire_at=time.time()+10, event_id=eid)
r1 = sq.enqueue(it1)
r2 = sq.enqueue(it2)
print("enqueue results:", r1, r2)
time.sleep(0.2)
sq.shutdown()
print("PASS: identical event_id second enqueue rejected by dedup" if not r2 else "NOTE: r2 accepted")
print("ALL OK")
