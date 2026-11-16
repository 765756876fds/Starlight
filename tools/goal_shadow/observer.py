"""Explicit one-slot observer, not a production hook or persistent service."""
import threading
import time

from .contracts import Action, DecisionResult
from .decision import decide


class ShadowObserver:
    def __init__(self, adapter, *, budget=.6):
        self.adapter, self.budget = adapter, min(max(budget, .05), 1)
        self._lock = threading.Lock()
        self._thread = None
        self._epoch = 0
        self._closed = False
        self._result = None
        self._deadline = 0

    def observe(self, goal):
        with self._lock:
            if self._closed or self._thread is not None and self._thread.is_alive():
                return False  # No queue and never a second hung worker.
            self._epoch += 1
            epoch = self._epoch
            self._result = None
            self._deadline = time.monotonic() + self.budget
            self._thread = threading.Thread(target=self._run, args=(epoch, goal), daemon=True,
                                            name="starlight-b1-shadow")
            self._thread.start()
            return True

    def _run(self, epoch, goal):
        try:
            world = self.adapter.capture(self.budget)
            result = decide(goal, world, now=time.monotonic(), current_generation=self.adapter.generation())
        except Exception:
            # Do not serialize exception messages: they can contain titles/paths.
            result = DecisionResult(Action.NEEDS_CLARIFICATION, "observer_read_failed", (), ())
        with self._lock:
            if not self._closed and epoch == self._epoch and time.monotonic() <= self._deadline:
                self._result = result

    def poll(self):
        with self._lock:
            if time.monotonic() > self._deadline:
                self._result = None
            return self._result

    def close(self, timeout=.05):
        with self._lock:
            self._closed = True
            self._epoch += 1
            self._result = None
            thread = self._thread
        if thread is not None:
            thread.join(min(max(timeout, 0), .2))
        return thread is None or not thread.is_alive()
