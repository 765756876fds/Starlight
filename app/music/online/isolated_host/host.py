"""Opt-in music source registration and bounded ownership of isolated sessions."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import threading
import time
from concurrent.futures import TimeoutError as CallTimeout
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logger import get_logger

from ..catalog_routes import validate_routes
from ..plugin_trust import PluginTrustStore, _owned_directory, _pinned_runtime, _plain_file
from ..source_authority import SourceAuthority, SourceLease
from . import win32
from .session import Session
from .settings import MAX_ISOLATED_SOURCES, IsolatedSourceSettings
from .source import IsolatedMusicSource


logger = get_logger()


@dataclass
class _Source:
    settings: IsolatedSourceSettings
    source: IsolatedMusicSource
    lease: SourceLease
    session: Session | None = None
    digest: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    calls: int = 0
    last_call: float = 0.0
    retry_after: float = 0.0
    removed: bool = False


def _copy_runtime(node: Path, target: Path, expected: str):
    node = _owned_directory(node.parent) / node.name
    before = _plain_file(node)
    digest = hashlib.sha256()
    with _pinned_runtime(node) as source, target.open("xb") as copy:
        if not os.path.samestat(before, os.fstat(source.fileno())):
            raise ValueError("isolated_runtime_changed")
        while block := source.read(1024 * 1024):
            digest.update(block)
            copy.write(block)
    if digest.hexdigest() != expected:
        raise ValueError("isolated_runtime_unapproved")


class IsolatedMusicHost:
    MAX_PROCESSES = MAX_ISOLATED_SOURCES
    MAX_CALLS = 12
    CALLS_PER_PLUGIN = 3
    START_TIMEOUT = 3.0
    AUDIT_INTERVAL = 0.5
    FAILURE_BACKOFF = 1.0

    def __init__(self, manager, settings, *, playback_holds_lease):
        self._manager = manager
        self._settings = settings
        self._playback_holds_lease = playback_holds_lease
        self._authority = SourceAuthority()
        self._slots = threading.BoundedSemaphore(self.MAX_CALLS)
        self._ingress = threading.Lock()
        self._shutdown = threading.Event()
        self._sources = {}
        self._active_calls = set()
        self._loop = None
        self._thread = None
        self._close_future = None
        self._closed_cleanly = False
        if not 0 < len(settings.sources) <= self.MAX_PROCESSES:
            raise ValueError("isolated_host_sources")
        validate_routes(settings.catalog_routes, settings.sources)
        parent = _owned_directory(settings.work_parent)
        trust_root = _owned_directory(settings.trust_root)
        project = Path(__file__).resolve().parents[4]
        if (os.name != "nt" or parent.is_relative_to(project) or trust_root.is_relative_to(project)
                or trust_root == parent or parent.is_relative_to(trust_root)):
            raise ValueError("isolated_host_workspace")
        self._root = win32.create_workspace(parent)
        try:
            self._runtime = self._root / "runtime" / "node.exe"
            self._runtime.parent.mkdir()
            pins = {settings.policy.plugins[source.name].runtime_sha256 for source in settings.sources}
            if len(pins) != 1:
                raise ValueError("isolated_runtime_pin")
            _copy_runtime(settings.node, self._runtime, pins.pop())
            self._trust = PluginTrustStore(trust_root, settings.policy, node=self._runtime)
            self._bootstrap = Path(__file__).with_name("bootstrap.js")
            for spec in settings.sources:
                self._register(spec)
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run, name="isolated-music-host", daemon=True)
            self._thread.start()
        except BaseException:
            for name in self._sources:
                self._manager.unregister(name)
            if self._loop is not None:
                self._loop.close()
            self._remove_workspace()
            raise

    def _bind(self, spec):
        return self._authority.bind(spec.name, spec.name, self._settings.policy.revision, spec.policies,
                                    test_loopback_origins=spec.test_loopback_origins)

    def _register(self, spec):
        if spec.name in self._sources or self._manager.get_source(spec.name) is not None:
            raise ValueError("isolated_host_duplicate_source")
        lease = self._bind(spec)
        source = IsolatedMusicSource(
            spec.name, spec.plugin_type, spec.providers,
            lambda method, params, timeout: self._invoke(spec.name, method, params, timeout),
            close=lambda: self._unregistered(spec.name),
        )
        self._sources[spec.name] = _Source(spec, source, lease)
        self._manager.register(source, lease=lease)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._audit_tasks = tuple(self._loop.create_task(self._watch(entry)) for entry in self._sources.values())
        try:
            self._loop.run_forever()
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
        finally:
            self._loop.close()

    def _invoke(self, name, method, params, timeout):
        with self._ingress:
            if self._shutdown.is_set() or not self._slots.acquire(blocking=False):
                raise RuntimeError("isolated_host_busy")
            try:
                future = asyncio.run_coroutine_threadsafe(self._call(name, method, params, timeout), self._loop)
            except RuntimeError:
                self._slots.release()
                raise
        # The caller's wait may finish before a real pipe or verifier does.
        # Retain the slot until the coroutine confirms that it has finished.
        future.add_done_callback(lambda _: self._slots.release())
        try:
            return future.result(timeout + self.START_TIMEOUT + 2.0)
        except CallTimeout:
            raise RuntimeError("isolated_host_timeout") from None

    def _current(self, name, digest):
        try:
            status = self._trust.status(name)
        except Exception:
            return False
        return bool(status and status["active_sha"] and status["active_policy"] == self._settings.policy.sha256
                    and not status["latest_revoked"]
                    and (digest is None or status["active_sha"] == digest))

    async def _check_current(self, entry):
        return await asyncio.to_thread(self._current, entry.settings.name, entry.digest)

    async def _open(self, entry):
        if entry.session is not None and not entry.session.stopping.is_set() and not entry.session.closed:
            return entry.session
        if (entry.removed or self._shutdown.is_set() or not entry.lease.active()
                or time.monotonic() < entry.retry_after):
            raise RuntimeError("isolated_source_unavailable")
        # Count startup and failed cleanup as well as active children.
        if entry.session is not None or sum(item.session is not None for item in self._sources.values()) >= self.MAX_PROCESSES:
            raise RuntimeError("isolated_host_busy")
        spec = entry.settings
        bundle = await asyncio.to_thread(self._trust.enabled, spec.name)
        if self._shutdown.is_set() or not entry.lease.active():
            raise RuntimeError("isolated_source_unavailable")
        # Another plugin may have reserved the last slot during signature verification.
        if sum(item.session is not None for item in self._sources.values()) >= self.MAX_PROCESSES:
            raise RuntimeError("isolated_host_busy")
        entry.digest = bundle.package_sha256
        config = {"origin": spec.test_loopback_origins[0]} if spec.test_loopback_origins else None
        session = Session(base=self._root, runtime=self._runtime, bootstrap=self._bootstrap, bundle=bundle,
                          lease=entry.lease, plugin_type=spec.plugin_type, providers=spec.providers, config=config,
                          resume_allowed=lambda: self._current(spec.name, bundle.package_sha256))
        entry.session = session
        try:
            await session.start(timeout=self.START_TIMEOUT)
            if self._shutdown.is_set() or not entry.lease.active() or not await self._check_current(entry):
                raise RuntimeError("isolated_source_revoked")
        except BaseException:
            await self._retire(entry, "isolated_start_failed", busy=True)
            raise
        return session

    async def _call(self, name, method, params, timeout):
        task = asyncio.current_task()
        entry = self._sources[name]
        if self._shutdown.is_set() or entry.removed or entry.calls >= self.CALLS_PER_PLUGIN:
            raise RuntimeError("isolated_source_busy")
        self._active_calls.add(task)
        entry.calls += 1
        session, lease = None, None
        try:
            async with entry.lock:
                if not await self._check_current(entry):
                    await self._retire(entry, "isolated_trust_changed")
                    raise RuntimeError("isolated_source_unavailable")
                session = await self._open(entry)
                lease = entry.lease
            result = await session.call(method, params, timeout=timeout)
            if not await self._check_current(entry):
                lease.retire()
            if not lease.active() or lease is not entry.lease or session is not entry.session:
                raise RuntimeError("isolated_source_revoked")
            return result
        except Exception:
            if session is not None and session.stopping.is_set():
                lease.retire()
            if entry.session is None:
                entry.retry_after = time.monotonic() + self.FAILURE_BACKOFF
            raise
        finally:
            entry.last_call = time.monotonic()
            entry.calls -= 1
            self._active_calls.discard(task)

    async def _retire(self, entry, reason, *, busy=False):
        entry.lease.retire()
        entry.retry_after = time.monotonic() + self.FAILURE_BACKOFF if busy else 0.0
        if entry.session is not None:
            status = await entry.session.stop(reason=reason)
            if not status["resources_released"]:
                raise RuntimeError("isolated_cleanup_pending")
            entry.session = None
        entry.digest = None

    async def _audit_one(self, entry):
        async with entry.lock:
            if entry.removed:
                await self._retire(entry, "isolated_source_unregistered")
                return
            if self._shutdown.is_set():
                return
            session = entry.session
            if session is not None:
                expired = (not entry.calls and time.monotonic() - entry.last_call >= self._settings.idle_timeout
                           and not entry.lease.busy() and not self._playback_holds_lease(entry.lease))
                if expired or not entry.lease.active() or session.stopping.is_set() or session.closed:
                    await self._retire(entry, "isolated_session_retired", busy=not expired)
            if (entry.session is None and not entry.lease.active() and time.monotonic() >= entry.retry_after
                    and await self._check_current(entry)):
                new = self._bind(entry.settings)
                if self._manager.renew_source_lease(entry.source, entry.lease, new):
                    entry.lease = new
                else:
                    new.retire()

    async def _watch(self, entry):
        while not self._shutdown.is_set():
            await asyncio.sleep(self.AUDIT_INTERVAL)
            try:
                # Revoke the matching capability before waiting on instance I/O.
                lease = entry.lease
                if entry.session is not None and lease.active() and not await self._check_current(entry):
                    lease.retire()
                await self._audit_one(entry)
            except Exception as error:
                logger.warning("[Music] Isolated session maintenance pending (%s)", type(error).__name__)

    def _unregistered(self, name):
        entry = self._sources.get(name)
        if entry is None or self._loop is None or self._shutdown.is_set():
            return
        entry.lease.retire()
        self._loop.call_soon_threadsafe(self._mark_removed, entry)

    def _mark_removed(self, entry):
        entry.removed = True

    async def _stop(self):
        await asyncio.gather(*(entry.session.stop(reason="isolated_host_exit")
                               for entry in self._sources.values() if entry.session is not None),
                             return_exceptions=True)
        await asyncio.gather(*self._audit_tasks, return_exceptions=True)
        await asyncio.gather(*self._active_calls, return_exceptions=True)
        results = await asyncio.gather(*(self._retire(entry, "isolated_host_exit")
                                         for entry in self._sources.values()), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            return False
        return True

    def _remove_workspace(self):
        candidate = self._root.resolve(strict=True)
        if candidate.parent != self._settings.work_parent.resolve(strict=True) or candidate != self._root:
            raise ValueError("isolated_cleanup_path")
        shutil.rmtree(candidate)

    def close(self, timeout=12.0):
        if self._closed_cleanly:
            return True
        if self._close_future is None:
            with self._ingress:
                self._shutdown.set()
                for entry in self._sources.values():
                    entry.lease.retire()
                self._close_future = asyncio.run_coroutine_threadsafe(self._stop(), self._loop)
        try:
            stopped = self._close_future.result(timeout=timeout)
        except CallTimeout:
            return False
        if not stopped:
            self._close_future = None
            return False
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            return False
        self._remove_workspace()
        self._closed_cleanly = True
        return True
