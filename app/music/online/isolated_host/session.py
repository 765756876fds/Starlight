
"""Bounded AppContainer RPC session, adapted from the B23 verified prototype."""
from __future__ import annotations

import asyncio
import ctypes
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid

from ..download_probe import request_plugin_http
from ..plugin_trust import VerifiedPlugin, _owned_directory, _package_path, _pinned_runtime, _plain_file
from ..source_authority import DownloadTransportError, SourceLease

MAX_FRAME = 64 * 1024
MAX_CALLS = 4
MAX_NETWORK = 2
METHODS = frozenset({
    "ping", "listSources", "searchPage", "getUrl", "downloadUrl", "getLyric", "getCover",
})
_RUNTIME_ACL_LOCK = threading.Lock()


CALL_ID = re.compile(r"[0-9a-f]{32}:[1-9][0-9]{0,15}", re.ASCII)
REQUEST_ID = re.compile(r"h[1-9][0-9]{0,14}", re.ASCII)


class Closed(RuntimeError):
    pass


def _unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON field")
        result[name] = value
    return result


def _reject_constant(value):
    raise ValueError("nonstandard JSON constant")


def _read_float(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("nonfinite JSON number")
    return value


def read_document(raw):
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields,
                          parse_constant=_reject_constant, parse_float=_read_float)
    if not isinstance(document, dict):
        raise ValueError("RPC document must be an object")
    return document


class FrameReader:
    def __init__(self):
        self.buffer = bytearray()
        self.peak = 0

    def feed(self, chunk):
        for index, part in enumerate(chunk.split(b"\n")):
            if index:
                if not self.buffer:
                    raise ValueError("empty frame")
                yield bytes(self.buffer)
                self.buffer.clear()
            if len(self.buffer) + len(part) > MAX_FRAME:
                raise ValueError("frame too large")
            self.buffer.extend(part)
            self.peak = max(self.peak, len(self.buffer))


@dataclass
class ActiveCall:
    method: str
    future: asyncio.Future
    deadline: float


class Session:
    def __init__(self, *, base: Path, runtime: Path, bootstrap: Path, bundle: VerifiedPlugin,
                 lease: SourceLease, plugin_type: str, providers: tuple[str, ...], config=None,
                 resume_allowed=None):
        if os.name != "nt":
            raise Closed("UNAVAILABLE")
        if (not isinstance(bundle, VerifiedPlugin) or not isinstance(lease, SourceLease)
                or not lease.active() or lease.plugin_id != bundle.plugin_id
                or plugin_type not in ("musicfree", "lx")):
            raise Closed("UNAUTHORIZED")
        from . import win32 as w

        self.win32 = w
        self.base = Path(base)
        self.runtime = Path(runtime)
        self.bootstrap = Path(bootstrap)
        self.bundle = bundle
        self.lease = lease
        self.plugin_type = plugin_type
        self.providers = tuple(providers)
        self.config = config or {}
        self._resume_allowed = resume_allowed
        self.origin = self.config.get("origin", "")
        if not isinstance(self.origin, str):
            raise ValueError("invalid owner origin")
        self.generation = uuid.uuid4().hex
        self.workspace = self.base / self.generation
        self._launch_task = None
        self._cleanup_scheduled = False
        self._dns_workers = {}
        self._dns_lock = threading.Lock()
        self.loop = asyncio.get_running_loop()
        self.ready = self.loop.create_future()
        self.capabilities = {}
        self.frame_reader = FrameReader()
        self.inbox = queue.Queue(32)
        self.outbox = queue.Queue(16)
        self.pending: dict[str, ActiveCall] = {}
        self.network = {}
        self.last_host_id = 0
        self.counter = 0
        self.wakeup = asyncio.Event()
        self.stopping = threading.Event()
        self.wrap_wakeup = threading.Event()
        self.stop_lock = asyncio.Lock()
        self.workers = []
        self.pump = None
        self.reaper = None
        self.profile = None
        self.owns_profile = False
        self.runtime_granted = False
        self.workspace_created = False
        self.launch = None
        self.job = None
        self.exit_code = None
        self.os_exit_s = None
        self.closed = False
        self.reason = None
        self.release = None
        self.pipe_fault = None
        self.late_messages = 0
        self.stderr_bytes = 0
        self.stderr_markers = set()
        self.start_error = None
        self.log_messages = 0

    async def start(self, timeout=4):
        if self._launch_task is not None or self.stopping.is_set():
            raise Closed("session cannot be restarted")
        self._launch_task = asyncio.create_task(asyncio.to_thread(self._start_local))
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(self._launch_task)
                if self.stopping.is_set():
                    raise Closed(self.reason or "START_FAILED")
                self.pump = asyncio.create_task(self._pump())
                ready = await asyncio.shield(self.ready)
            if ready is None:
                raise Closed(self.reason or "READY_FAILED")
            self.capabilities = ready["capabilities"]
        except BaseException as error:
            self.start_error = type(error).__name__
            await asyncio.shield(self.stop("START_FAILED"))
            raise

    def _require_starting(self):
        if self.stopping.is_set() or not self.lease.active():
            raise Closed("START_CANCELLED")

    def _require_resume(self):
        if self._resume_allowed is not None and not self._resume_allowed():
            raise Closed("START_UNAUTHORIZED")
        self._require_starting()

    def _stage_plugin(self, root):
        if self.bundle.entry not in self.bundle.files:
            raise Closed("MANIFEST_ENTRY")
        for name, contents in self.bundle.files.items():
            if not _package_path(name) or type(contents) is not bytes:
                raise Closed("MANIFEST_FILE")
            path = root.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as target:
                target.write(contents)
        index = root.parent / "verified-files.json"
        with index.open("x", encoding="utf-8") as target:
            json.dump(sorted(self.bundle.files), target, ensure_ascii=True, separators=(",", ":"))
        return root.joinpath(*PurePosixPath(self.bundle.entry).parts), index

    def _start_local(self):
        w = self.win32
        self._require_starting()
        self.base = _owned_directory(self.base)
        self.workspace = self.base / self.generation
        runtime_dir = _owned_directory(self.runtime.parent)
        if runtime_dir == self.base or not runtime_dir.is_relative_to(self.base):
            raise Closed("RUNTIME_LAYOUT")
        self.runtime = runtime_dir / self.runtime.name
        runtime_info = _plain_file(self.runtime)
        _owned_directory(self.bootstrap.parent)
        _plain_file(self.bootstrap)
        self.workspace.mkdir()
        self.workspace_created = True
        code_dir = self.workspace / "code"
        package_root = code_dir / "package"
        plugin, index = self._stage_plugin(package_root)
        # Stage the host-owned loader beside, and outside, the signed plugin
        # package. Its read-only ACL is shared with the package directory.
        with _pinned_runtime(self.bootstrap) as source:
            code = source.read(64 * 1024 + 1)
        if not 0 < len(code) <= 64 * 1024:
            raise Closed("trusted bootstrap too large")
        staged_bootstrap = code_dir / "bootstrap.cjs"
        with staged_bootstrap.open("xb") as output:
            output.write(code)
        work_dir = self.workspace / "work"
        work_dir.mkdir()

        self._require_starting()
        self.profile = w.ensure_profile("SL25" + self.generation)
        self.owns_profile = not self.profile.already_existed and self.profile.create_hr == "0x00000000"
        if not self.owns_profile or not self.profile.sid_string:
            raise Closed("fresh AppContainer profile unavailable")
        for path, perm in ((runtime_dir, "RX"), (code_dir, "RX"), (work_dir, "M")):
            self._require_starting()
            with _RUNTIME_ACL_LOCK:
                if path == runtime_dir:
                    self.runtime_granted = True
                grant = w.grant_acl(path, self.profile.sid_string, perm, workspace_root=self.base)
            if grant["rc"] != 0:
                raise Closed("ACL grant failed")

        self.job = w.Job(active_processes=1, process_memory_mb=256, job_memory_mb=256)
        provider_json = json.dumps(self.providers, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        config_json = json.dumps(self.config, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(provider_json) > 2048 or len(config_json) > 4096 or "\x00" in self.origin or len(self.origin) > 1024:
            raise Closed("OWNER_CONFIG_SIZE")
        env = {
            "STARLIGHT_BOOTSTRAP": str(staged_bootstrap),
            "STARLIGHT_PLUGIN_PATH": str(plugin),
            "STARLIGHT_PLUGIN_ROOT": str(package_root),
            "STARLIGHT_PLUGIN_FILES": str(index),
            "STARLIGHT_PLUGIN_TYPE": self.plugin_type,
            "STARLIGHT_PLUGIN_ID": self.bundle.plugin_id,
            "STARLIGHT_SOURCE_ID": self.lease.source_id,
            "STARLIGHT_PLUGIN_ORIGIN": self.origin,
            "STARLIGHT_PLUGIN_CONFIG": config_json,
            "STARLIGHT_LX_SOURCES": provider_json,
        }
        # Node's script entry resolution inspects ancestors that the child may
        # not list. Open the exact pinned loader by its host-provided path.
        cmd = subprocess.list2cmdline([str(self.runtime), "-e",
            "eval(require('node:fs').readFileSync(process.env.STARLIGHT_BOOTSTRAP,'utf8'))"])
        if len(cmd.encode("utf-16-le")) // 2 >= 32767:
            raise Closed("command line too long")
        self._require_starting()
        with _pinned_runtime(self.runtime) as stream, _pinned_runtime(staged_bootstrap):
            opened = os.fstat(stream.fileno())
            if (not os.path.samestat(runtime_info, opened)
                    or not os.path.samestat(opened, _plain_file(self.runtime))
                    or hashlib.file_digest(stream, "sha256").hexdigest() != self.bundle.runtime_sha256):
                raise Closed("RUNTIME_INTEGRITY")
            self.launch = w.launch_appcontainer(node_path=self.runtime, command_line=cmd,
                                                cwd=work_dir, profile=self.profile, env_extra=env,
                                                job=self.job, require_job=True,
                                                before_resume=self._require_resume)
        if self.launch.error or not self.launch.process:
            raise Closed(self.launch.error or "AppContainer start failed")
        for kind in ("stdout", "stderr", "stdin"):
            target = self._writer if kind == "stdin" else self._reader
            worker = threading.Thread(target=target, args=(kind,), daemon=True, name=f"sl25-{kind}")
            worker.start()
            self.workers.append(worker)

    def _notify(self):
        if not self.wrap_wakeup.is_set():
            self.wrap_wakeup.set()
            self.loop.call_soon_threadsafe(self.wakeup.set)

    def _fail_pipe(self, reason):
        if not self.stopping.is_set() and self.pipe_fault is None:
            self.pipe_fault = reason
            self._notify()

    def _reader(self, kind):
        w = self.win32
        handle = self.launch.parent_handles[kind]
        chunk = ctypes.create_string_buffer(4096)
        done = w.wintypes.DWORD()
        try:
            while not self.stopping.is_set():
                if not w.kernel32.ReadFile(handle, chunk, len(chunk), ctypes.byref(done), None) or not done.value:
                    break
                if kind == "stderr":
                    # Never retain plugin text, which can contain credentials or signed URLs.
                    self.stderr_bytes = min(8192, self.stderr_bytes + done.value)
                    data = chunk.raw[:done.value]
                    for marker in (b"MODULE_NOT_FOUND", b"EACCES", b"EPERM", b"SyntaxError",
                                   b"Cannot find module", b"ENOENT", b"Error"):
                        if marker in data:
                            self.stderr_markers.add(marker.decode("ascii"))
                    continue
                for frame in self.frame_reader.feed(chunk.raw[:done.value]):
                    self.inbox.put_nowait(frame)
                    self._notify()
        except (ValueError, queue.Full):
            self._fail_pipe("STRUCTURE")
        finally:
            if kind == "stdout":
                self._fail_pipe("PROCESS_EXITED")

    def _writer(self, kind):
        w = self.win32
        handle = self.launch.parent_handles[kind]
        done = w.wintypes.DWORD()
        while not self.stopping.is_set():
            try:
                data = self.outbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if self.stopping.is_set():
                break
            if data is None:
                break
            if not w.kernel32.WriteFile(handle, data, len(data), ctypes.byref(done), None) or done.value != len(data):
                self._fail_pipe("WRITE_FAILED")
                break

    def _send(self, msg):
        encoded = json.dumps({"v": 1, **msg}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > MAX_FRAME:
            raise Closed("STRUCTURE")
        try:
            self.outbox.put_nowait(encoded + b"\n")
        except queue.Full as error:
            raise Closed("WRITE_QUEUE_FULL") from error

    async def _pump(self):
        while not self.stopping.is_set():
            await self.wakeup.wait()
            self.wakeup.clear()
            self.wrap_wakeup.clear()
            if self.pipe_fault:
                self.reaper = asyncio.create_task(self.stop(self.pipe_fault))
                return
            try:
                # Yield between bounded batches so RPC cannot indefinitely starve deadlines.
                for _ in range(32):
                    try:
                        frame = self.inbox.get_nowait()
                    except queue.Empty:
                        break
                    self._dispatch(read_document(frame))
            except (ValueError, TypeError, RecursionError, Closed):
                self.reaper = asyncio.create_task(self.stop("STRUCTURE"))
                return
            if not self.inbox.empty():
                self.wakeup.set()
            await asyncio.sleep(0)

    def _dispatch(self, msg):
        if type(msg.get("v")) is not int or msg["v"] != 1:
            raise ValueError("frame version")
        kind = msg.get("type")
        if not self.ready.done():
            expected = {
                "type": "ready", "pid": self.launch.process.dwProcessId,
                "plugin_id": self.bundle.plugin_id, "origin": self.origin,
                "plugin_type": self.plugin_type,
            }
            if kind == "log":
                self.log_messages = min(32, self.log_messages + 1)
                return
            if (type(msg.get("pid")) is not int or any(msg.get(key) != val for key, val in expected.items())
                    or msg.get("source_name", self.lease.source_id) != self.lease.source_id
                    or not isinstance(msg.get("capabilities"), dict)):
                raise ValueError("invalid ready")
            self.ready.set_result(msg)
            return
        if kind == "log":
            self.log_messages = min(32, self.log_messages + 1)
            return
        if kind not in ("result", "host_request"):
            raise ValueError("unknown frame type")
        frame_id = msg.get("id")
        call_id = frame_id if kind == "result" else msg.get("call_id")
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id) or not isinstance(frame_id, str):
            raise ValueError("frame ID")
        if kind == "result" and type(msg.get("ok")) is not bool:
            raise ValueError("invalid result status")
        call = self.pending.get(call_id)
        if call is None or call.future.done() or time.perf_counter() >= call.deadline:
            self.late_messages = min(1024, self.late_messages + 1)
            return
        if kind == "result":
            value = msg.get("result") if msg["ok"] else None
            allow_empty = call.method in ("getUrl", "downloadUrl", "getLyric", "getCover")
            if msg["ok"] and not isinstance(value, dict) and not (allow_empty and value is None):
                raise ValueError("invalid result value")
            if any(owner == call_id for owner, _ in self.network.values()):
                raise ValueError("result precedes owned network completion")
            failure = msg.get("error")
            if not msg["ok"]:
                value = ("STRUCTURE" if failure in ("STRUCTURE_SEARCH", "STRUCTURE_KEYWORD", "STRUCTURE_PAGE",
                                                    "PROTOCOL_OVERSIZED") else "PLUGIN")
            call.future.set_result((msg["ok"], value))
            return
        request = msg.get("request")
        if not isinstance(request, dict) or not REQUEST_ID.fullmatch(frame_id):
            raise ValueError("invalid host request")
        number = int(frame_id[1:])
        if number <= self.last_host_id:
            raise ValueError("duplicate host request ID")
        self.last_host_id = number
        with self._dns_lock:
            active_requests = self.network.keys() | self._dns_workers.values()
        if len(active_requests) >= MAX_NETWORK:
            self._send({"type": "host_response", "id": frame_id, "call_id": call_id,
                        "ok": False, "error": {"code": "BUSY"}})
            return
        task = asyncio.create_task(self._network(frame_id, call_id, request, call.deadline))
        self.network[frame_id] = call_id, task

    def _track_dns_worker(self, frame_id, worker):
        with self._dns_lock:
            self._dns_workers[worker] = frame_id
        worker.add_done_callback(self._dns_finished)

    def _dns_finished(self, worker):
        with self._dns_lock:
            self._dns_workers.pop(worker, None)

    def _dns_count(self):
        with self._dns_lock:
            return len(self._dns_workers)

    async def _network(self, frame_id, call_id, request, deadline):
        response = {"type": "host_response", "id": frame_id, "call_id": call_id}
        try:
            if (set(request) - {"url", "method", "headers", "body", "response_type"}
                    or request.get("response_type", "text") not in ("text", "json")):
                raise DownloadTransportError("host_request_shape")
            body = request.get("body")
            if body is not None:
                if not isinstance(body, str):
                    raise DownloadTransportError("host_request_shape")
                body = body.encode("utf-8")
            resource = self.lease.resource(
                "host_http", request.get("url"), request.get("headers"),
                resource_id=f"{call_id}/{frame_id}",
            )
            # Hold the broker deadline below the bootstrap's 6.5 s Promise
            # timeout, so a normal transport timeout can settle the plugin call.
            async with asyncio.timeout(max(0, min(6, deadline - time.perf_counter()))):
                status, content_type, data = await request_plugin_http(
                    resource, request.get("method", "GET"), body=body,
                    cancelled=self.stopping.is_set,
                    on_dns_worker=lambda worker: self._track_dns_worker(frame_id, worker),
                )
            response.update(ok=True, result={
                "status": status,
                "headers": {"content-type": content_type},
                "content_type": content_type,
                "body": data.decode("utf-8"),
                "encoding": "utf8",
            })
            if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > MAX_FRAME - 64:
                response.pop("result")
                raise DownloadTransportError("host_response_size")
        except DownloadTransportError as error:
            response.update(ok=False, error={"code": error.reason})
        except TimeoutError:
            response.update(ok=False, error={"code": "timeout"})
        except Exception:
            response.update(ok=False, error={"code": "HOST_NETWORK_FAILED"})
        finally:
            self.network.pop(frame_id, None)
        parent = self.pending.get(call_id)
        if (not self.stopping.is_set() and self.lease.active()
                and parent is not None and not parent.future.done()
                and time.perf_counter() < deadline):
            try:
                self._send(response)
            except (ValueError, Closed):
                self._fail_pipe("STRUCTURE")

    async def call(self, method, params, timeout=3):
        if (self.stopping.is_set() or not self.lease.active()
                or not self.ready.done() or self.ready.cancelled() or self.ready.result() is None):
            raise Closed("UNAVAILABLE")
        if method not in METHODS or not isinstance(params, dict) or not 0 < timeout <= 15:
            raise ValueError("unsupported call")
        if len(self.pending) >= MAX_CALLS:
            raise Closed("BUSY")
        self.counter += 1
        call_id = f"{self.generation}:{self.counter}"
        call = ActiveCall(method, self.loop.create_future(), time.perf_counter() + timeout)
        self.pending[call_id] = call
        try:
            self._send({"type": "call", "id": call_id, "method": method, "params": params})
            accepted, value = await asyncio.wait_for(asyncio.shield(call.future), timeout)
            if time.perf_counter() >= call.deadline:
                raise TimeoutError
            if not accepted:
                raise Closed(self.reason or value or "PLUGIN")
            if self.stopping.is_set() or not self.lease.active():
                raise Closed("UNAUTHORIZED")
            return value
        except TimeoutError as error:
            await self.stop("TIMEOUT")
            raise Closed("TIMEOUT") from error
        except asyncio.CancelledError:
            await asyncio.shield(self.stop("CANCELLED"))
            raise
        finally:
            self.pending.pop(call_id, None)
            if not call.future.done():
                call.future.cancel()

    def _request_io_cancel(self):
        w = self.win32
        for worker in self.workers:
            if worker.is_alive():
                thread = w.kernel32.OpenThread(1, False, worker.native_id)
                if thread:
                    w.kernel32.CancelSynchronousIo(thread)
                    w.kernel32.CloseHandle(thread)

    def _job_count(self):
        if not self.job or not self.job.handle:
            return 0
        return self.job.active_count()

    def _revoke_runtime_acl(self):
        if not self.runtime_granted:
            return True
        try:
            with _RUNTIME_ACL_LOCK:
                removed = self.win32.remove_acl(self.runtime.parent, self.profile.sid_string,
                                                workspace_root=self.base)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
            return False
        if removed:
            self.runtime_granted = False
        return not self.runtime_granted

    def _remove_workspace(self):
        if not self.workspace_created:
            return True
        if self.workspace.resolve().parent != self.base.resolve():
            return False
        try:
            info = self.workspace.lstat()
            if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return False
            shutil.rmtree(self.workspace)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        self.workspace_created = False
        return True

    def _reap_after_launch(self, task):
        # The worker may finish after the stop budget. Retain this session
        # until cleanup has actually run, even if the launcher returned late.
        if not task.cancelled():
            task.exception()
        self.reaper = asyncio.create_task(self.stop(self.reason or "START_CANCELLED"))

    async def stop(self, reason="HOST_STOP"):
        w = self.win32
        async with self.stop_lock:
            if self.closed:
                return self.release
            t0 = time.perf_counter()
            self.reason = self.reason or reason
            self.stopping.set()
            self.wakeup.set()
            if not self.ready.done():
                self.ready.set_result(None)
            for call in self.pending.values():
                if not call.future.done():
                    call.future.set_result((False, None))
            if self._launch_task is not None and not self._launch_task.done():
                await asyncio.wait((self._launch_task,), timeout=2)
                if not self._launch_task.done():
                    if not self._cleanup_scheduled:
                        self._cleanup_scheduled = True
                        self._launch_task.add_done_callback(self._reap_after_launch)
                    self.release = {
                        "reason": self.reason, "resources_released": False, "startup_released": False,
                        "release_s": time.perf_counter() - t0,
                    }
                    return self.release
            tasks = [task for _, task in self.network.values()]
            for task in tasks:
                task.cancel()
            # A never-started task cannot run its coroutine finally.
            try:
                self.outbox.put_nowait(None)
            except queue.Full:
                pass
            process = self.launch.process if self.launch else None
            handle = process.hProcess if process else None
            self.job_terminate_ok = self.job.terminate() if self.job and self.job.assigned else None
            if handle and self.launch.error and self.job_terminate_ok is None:
                w.kernel32.TerminateProcess(handle, 1)
            deadline = time.perf_counter() + 2
            process_exited = not handle
            active_job_processes = self._job_count()
            handles_closed = False
            workers_stopped = False
            network_released = False
            while True:
                now = time.perf_counter()
                if not process_exited and w.kernel32.WaitForSingleObject(handle, 0) == w.WAIT_OBJECT_0:
                    process_exited = True
                    self.os_exit_s = now - t0
                    exit_code = w.wintypes.DWORD()
                    if w.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        self.exit_code = exit_code.value
                workers_stopped = not any(worker.is_alive() for worker in self.workers)
                for key, (_, task) in tuple(self.network.items()):
                    if task.done():
                        self.network.pop(key)
                scope_status = {"pending": len(self.network), "dns_workers": self._dns_count()}
                network_released = not self.network and scope_status["dns_workers"] == 0
                if workers_stopped and process_exited and not handles_closed:
                    handles_closed = not self.launch or self.launch.close_handles()
                active_job_processes = self._job_count()
                pump_stopped = self.pump is None or self.pump.done()
                released = handles_closed and active_job_processes == 0 and network_released and pump_stopped
                if released or now >= deadline:
                    break
                self._request_io_cancel()
                await asyncio.sleep(0.01)
            for worker in self.workers:
                if not worker.is_alive():
                    worker.join()
            job_closed = not self.job or (process_exited and active_job_processes == 0 and self.job.close())
            runtime_acl_removed = process_exited and await asyncio.to_thread(self._revoke_runtime_acl)
            workspace_removed = handles_closed and job_closed and await asyncio.to_thread(self._remove_workspace)
            profile_deleted = not self.owns_profile
            if self.owns_profile and process_exited and job_closed and handles_closed:
                rc = await asyncio.to_thread(w.delete_profile, self.profile.name)
                profile_deleted = rc == "0x00000000"
                if profile_deleted:
                    self.owns_profile = False
            if self.profile and profile_deleted and runtime_acl_removed:
                self.profile.close()
            pump_stopped = self.pump is None or self.pump.done()
            self.closed = bool(handles_closed and job_closed and profile_deleted and network_released
                               and runtime_acl_removed and workspace_removed and pump_stopped)
            if self.closed:
                self.workers.clear()
                for messages in (self.inbox, self.outbox):
                    while not messages.empty():
                        messages.get_nowait()
            self.release = {
                "process_exited": process_exited, "startup_released": True,
                "exit_code": self.exit_code, "exit_s": self.os_exit_s,
                "workers_stopped": workers_stopped, "handles_closed": bool(handles_closed),
                "job_closed": bool(job_closed), "job_terminate_ok": self.job_terminate_ok,
                "active_job_processes": active_job_processes, "runtime_acl_removed": runtime_acl_removed,
                "profile_deleted": profile_deleted, "network_released": network_released, "scope": scope_status,
                "workspace_removed": bool(workspace_removed), "resources_released": self.closed,
                "release_s": time.perf_counter() - t0, "reason": self.reason,
                "pump_stopped": self.pump is None or self.pump.done(),
                "stderr_bytes": self.stderr_bytes, "stderr_markers": sorted(self.stderr_markers),
            }
            return self.release
