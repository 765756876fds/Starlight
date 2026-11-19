"""Session-local playback evidence. Search responses never enter this ledger."""
from collections import OrderedDict, deque
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
import time


@dataclass(frozen=True)
class PlaybackStartup:
    attempt: int
    generation: int
    episode: int
    state: str
    media_tag: str
    candidate_tag: str


class StartupEvidence:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = Lock()
        self._sources = OrderedDict()

    def observe(self, source, media_tag, started):
        # Keep only fingerprints in memory, with no URLs or titles.
        key = sha256(source.encode()).digest()
        with self._lock:
            record = self._sources.setdefault(key, deque(maxlen=8))
            record.append((self._clock(), media_tag, started))
            self._sources.move_to_end(key)
            if len(self._sources) > 128:
                self._sources.popitem(last=False)

    def rank(self, source):
        key = sha256(source.encode()).digest()
        now = self._clock()
        with self._lock:
            recent = {tag: success for stamp, tag, success in self._sources.get(key, ())
                      if 0 <= now - stamp < 600}
        # Require evidence from distinct media before affecting another search.
        if len(recent) < 2:
            return 0
        if all(recent.values()):
            return -1
        if not any(recent.values()):
            return 1
        return 0
