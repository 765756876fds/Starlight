"""Bounded delivery of passive voice diagnostics, separate from command execution.

An active pair and the latest waiting pair are retained. A slow observer cannot
queue unlimited old transcripts or occupy the EventBus game-reminder executor.
The on-demand worker exits when drained; it never runs business commands.
"""
import logging
import threading
import time


class VoiceObservations:
    def __init__(self, publish):
        self._publish = publish
        self._lock = threading.Lock()
        self._pending = None
        self._worker = None
        self._closed = False
        self._revision = 0

    def submit(self, events):
        with self._lock:
            if self._closed:
                return
            self._pending = (self._revision, tuple(events))
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain,
                    name="voice-observations", daemon=True)
                self._worker.start()

    def close(self):
        # Never join a third-party callback on the GUI shutdown path.
        with self._lock:
            self._closed = True
            self._revision += 1
            self._pending = None

    def reopen(self):
        # Reuse an in-flight worker across stop/start, never stack blocked ones.
        with self._lock:
            self._closed = False

    def _drain(self):
        while True:
            with self._lock:
                if self._closed or self._pending is None:
                    self._worker = None
                    return
                (revision, events), self._pending = self._pending, None
            for event in events:
                with self._lock:
                    if self._closed or revision != self._revision:
                        break
                started = time.monotonic()
                try:
                    self._publish(event)
                except Exception:
                    logging.getLogger(__name__).exception("voice observation delivery failed")
                finally:
                    logging.getLogger(__name__).info(
                        "voice_timing stage=%s elapsed_ms=%.0f",
                        event.event_type, (time.monotonic() - started) * 1000)
