"""The optional checked handoff uses the existing search pool and quality strip."""
from collections import Counter
import logging
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Signal

from app.movie.playback_diagnostics import number
from app.movie.safe_switch import assess_context, context_current
from app.movie.switch_probe import PROBE_MS
from .latest_task import LatestTask


logger = logging.getLogger(__name__)
_REASONS = frozenset({"no_candidate", "identity_insufficient", "episode_mismatch",
                      "unsupported_hls", "unsupported_origin", "timeline_mismatch",
                      "source_unavailable", "candidate_too_slow", "budget_exceeded",
                      "content_mismatch", "verified"})


class MovieSwitchTrial(QObject):
    completed = Signal(object)

    def __init__(self, controller, parent):
        super().__init__(parent)
        self.controller = controller
        self.enabled = False
        self._task = LatestTask(self)
        self._task.completed.connect(self._finished)
        self.service = self.context = self.token = self.proof = None
        self.source_revision = None
        self.expires = 0.
        self._tried_generation = None
        self._switched_generation = None
        self._diagnosed_generation = None
        self._context_rejection = None
        self.reason_counts = Counter()

    @property
    def busy(self):
        return self._task._running

    def prepare(self, service, group):
        self.reset()
        if self.service is not service:
            if self.service is not None:
                self.service.pool.returned.disconnect(self._verified)
            self.service = service
            if service is not None:
                service.pool.returned.connect(self._verified)
        if service is not None:
            self.context, self._context_rejection = assess_context(self.controller, group)
        self.source_revision = service.manager.get_search_revision() if service and service.manager else None

    def reset(self):
        if self.token is not None:
            token, self.token = self.token, None
            self.service.pool.cancel(lambda key: key is token)
        self.context = self.proof = self._context_rejection = None
        self.expires = 0.

    def _record_reason(self, reason):
        reason = reason if isinstance(reason, str) and reason in _REASONS else "source_unavailable"
        self.reason_counts[reason] = min(65535, self.reason_counts[reason]+1)
        logger.info("movie_switch_check result=%s count=%d", reason, self.reason_counts[reason])

    def _sustained_report(self):
        observation = self.controller.source_quality_snapshot()
        report = observation.get("current") if observation else None
        return bool(report and report.sustained_transport_wait and report.state == "BUFFERING")

    def _sustained(self):
        if not self.enabled or self.context is None or not context_current(self.controller, self.context):
            return False
        manager = self.service.manager
        if manager is None or self.source_revision != manager.get_search_revision():
            return False
        return self._sustained_report()

    def poll(self, visible):
        generation = getattr(self.controller, "_playback_gen", None)
        if (visible and self.enabled and self._context_rejection
                and generation != self._diagnosed_generation and self._sustained_report()):
            self._diagnosed_generation = generation
            self._record_reason(self._context_rejection)
        if not visible or not self._sustained():
            if self.token is not None:
                token, self.token = self.token, None
                self.service.pool.cancel(lambda key: key is token)
            self.proof = None
            return False
        if self.proof is not None:
            if time.monotonic() < self.expires:
                return True
            self.proof = None
        context = self.context
        service = self.service
        if (not self.busy and context.generation not in (self._tried_generation, self._switched_generation)
                and self.token is None and not service.pool.active and service.request is None):
            self.token = object()
            self._tried_generation = context.generation
            local_hosts = {"127.0.0.1", "::1"}
            # Only an explicitly configured local CMS pair can opt into loopback.
            sources = service.manager.get_enabled_sources()
            loopback = (len(sources) >= 2 and all(urlsplit(s["url"]).hostname in local_hosts for s in sources)
                        and all(urlsplit(u).hostname in local_hosts for u in (context.original, context.target)))
            if not service.pool.start(self.token, dict(mode="verify_switch", loopback=loopback,
                                                       original=context.original, candidate=context.target), PROBE_MS):
                self.token = None
        return False

    def _verified(self, token, result):
        if token is not self.token:
            return
        self.token = None
        duration = number(result.get("duration"))
        if result.get("status") != "ok":
            self._record_reason("budget_exceeded" if result.get("error") == "Timeout" else result.get("reason"))
        if result.get("status") == "ok" and duration is not None and duration > 4 and self._sustained():
            self._record_reason("verified")
            self.proof = duration
            self.expires = time.monotonic() + 10

    def decline(self):
        self.proof = None

    def confirm(self):
        if (self.proof is None or self.busy or time.monotonic() >= self.expires or not self._sustained()):
            self.decline()
            return
        context, duration, expires = self.context, self.proof, self.expires
        self.decline()

        def switch(valid):
            def permit():
                return (valid() and self.enabled and self.context is context
                        and time.monotonic() < expires and self._sustained())
            return self.controller.switch_verified_source(context, duration, permit)
        self._task.submit(context, switch, guarded=True)

    def _finished(self, context, result, error):
        self.proof = None
        if result == "started":
            self._switched_generation = context.generation + 1
        self.completed.emit((context, result if error is None else "unknown"))
