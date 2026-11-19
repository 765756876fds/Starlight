"""The optional checked handoff uses the existing search pool and quality strip."""
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Signal

from app.movie.playback_diagnostics import number
from app.movie.safe_switch import context_current, context_for
from app.movie.switch_probe import PROBE_MS
from .latest_task import LatestTask


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
        self.context = context_for(self.controller, group) if service is not None else None
        self.source_revision = service.manager.get_search_revision() if service and service.manager else None

    def reset(self):
        if self.token is not None:
            token, self.token = self.token, None
            self.service.pool.cancel(lambda key: key is token)
        self.context = self.proof = None
        self.expires = 0.

    def _sustained(self):
        if not self.enabled or self.context is None or not context_current(self.controller, self.context):
            return False
        manager = self.service.manager
        if manager is None or self.source_revision != manager.get_search_revision():
            return False
        observation = self.controller.source_quality_snapshot()
        report = observation.get("current") if observation else None
        return bool(report and report.sustained_transport_wait and report.state == "BUFFERING")

    def poll(self, visible):
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
        if result.get("status") == "ok" and duration is not None and duration > 4 and self._sustained():
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
