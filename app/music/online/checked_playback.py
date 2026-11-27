"""Serve a checked single audio resource to the existing independent MPV process."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from secrets import token_urlsafe
import socket
from threading import BoundedSemaphore, Condition, Event, Lock, Thread

import aiohttp

from .download_probe import (
    DownloadTransportError, ProbeEvidence, _bounded, _headers, _identity_encoding,
    _positive_length, _record_prefix, _request, _session, _target,
)
from .source_authority import AuthorizedResource

_RANGE_REQUEST = re.compile(r"bytes=([0-9]{1,16})-([0-9]{0,16})")
_RANGE_REPLY = re.compile(r"bytes ([0-9]{1,16})-([0-9]{1,16})/([0-9]{1,16})")
_UNSATISFIED = re.compile(r"bytes \*/[0-9]{1,16}")


@dataclass
class _Stream:
    url: str = field(repr=False)
    headers: dict = field(repr=False)
    authorization: AuthorizedResource | None = field(default=None, repr=False)
    stopped: Event = field(default_factory=Event)
    proof: tuple[str, str | None] | None = field(default=None, repr=False)
    clients: set = field(default_factory=set, repr=False)

    def active(self):
        return not self.stopped.is_set() and (self.authorization is None or self.authorization.active())

    def stop(self):
        self.stopped.set()
        for client in self.clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _LocalAudioServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, streams):
        self.streams = streams
        self.slots = BoundedSemaphore(4)
        self.active = Condition()
        self.clients = set()
        super().__init__(("127.0.0.1", 0), _AudioRequest)

    def get_request(self):
        client, address = super().get_request()
        client.settimeout(5)
        return client, address

    def service_actions(self):
        # A receiver blocked on a full socket cannot poll from its relay task.
        self.streams.expire()

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self.active:
            self.clients.add(request)
        try:
            super().process_request(request, address)
        except BaseException:
            self._released(request)
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._released(request)

    def _released(self, request):
        self.slots.release()
        with self.active:
            self.clients.discard(request)
            self.active.notify_all()

    def stop_clients(self):
        with self.active:
            for client in self.clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return self.active.wait_for(lambda: not self.clients, timeout=1.5)

    def handle_error(self, request, address):
        # Upstream and loopback media addresses must never enter diagnostics.
        pass


class _AudioRequest(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self._serve("GET")

    def do_HEAD(self):
        self._serve("HEAD")

    def _serve(self, method):
        stream = self.server.streams.attach(self.path, self.connection)
        if stream is None:
            self.send_error(404)
            return
        try:
            self._serve_stream(stream, method)
        finally:
            self.server.streams.detach(stream, self.connection)

    def _serve_stream(self, stream, method):
        ranges = self.headers.get_all("Range", [])
        span = _RANGE_REQUEST.fullmatch(ranges[0]) if len(ranges) == 1 else None
        if ranges and (not span or span[2] and int(span[2]) < int(span[1])):
            self.send_error(416)
            return
        self._started = False
        timeout = 240 if stream.authorization is None else stream.authorization.budget.timeout
        try:
            asyncio.run(_bounded(self._relay(stream, method, span), timeout, lambda: not stream.active()))
        except (OSError, ValueError, EOFError, aiohttp.ClientError):
            if not self._started and stream.active():
                self.send_error(502)

    async def _relay(self, stream, method, span):
        headers = _headers(stream.headers)
        if span:
            headers["Range"] = span[0]
        options = {} if stream.authorization is None else {"authorization": stream.authorization}
        async with _session(12, 65536, **options) as client:
            async with _request(client, method, _target(stream.url, **options), headers, **options) as response:
                if response.status == 416:
                    self.send_response(416)
                    self._started = True
                    content_range = response.headers.get("Content-Range", "")
                    if _UNSATISFIED.fullmatch(content_range):
                        self.send_header("Content-Range", content_range)
                    self.end_headers()
                    return
                if response.status not in (200, 206) or not _identity_encoding(response):
                    raise DownloadTransportError("unavailable_audio")
                length = _positive_length(response.headers.get("Content-Length"))
                content_range = response.headers.get("Content-Range", "")
                offset = 0
                if response.status == 206:
                    reply = _RANGE_REPLY.fullmatch(content_range)
                    if not reply or not span:
                        raise DownloadTransportError("invalid_range")
                    start, end, total = map(int, reply.groups())
                    if (start != int(span[1]) or not start <= end < total
                            or span[2] and end > int(span[2])
                            or length is not None and length != end-start+1):
                        raise DownloadTransportError("invalid_range")
                    length, offset = end-start+1, start
                elif content_range:
                    raise DownloadTransportError("invalid_range")

                prefix = b""
                if method == "GET":
                    effective = (str(response.url), response.headers.get("ETag"))
                    if offset == 0 and (length is None or length >= 32 or stream.proof != effective):
                        amount = min(length or 8192, 8192)
                        try:
                            prefix = await response.content.readexactly(amount)
                        except asyncio.IncompleteReadError as exc:
                            if length is not None:
                                raise
                            prefix = exc.partial
                        evidence = ProbeEvidence()
                        _record_prefix(evidence, prefix)
                        if not evidence.container:
                            raise DownloadTransportError("unrecognized_audio")
                        stream.proof = effective
                    elif stream.proof != effective:
                        raise DownloadTransportError("unverified_range")
                self._started = True
                self.send_response(response.status)
                self.send_header("Content-Type", "application/octet-stream")
                if length:
                    self.send_header("Content-Length", str(length))
                if response.status == 206:
                    self.send_header("Content-Range", content_range)
                if response.headers.get("Accept-Ranges", "").lower() == "bytes":
                    self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if method == "GET":
                    sent = len(prefix)
                    self.wfile.write(prefix)
                    async for data in response.content.iter_chunked(65536):
                        if not stream.active() or length and sent + len(data) > length:
                            return
                        self.wfile.write(data)
                        sent += len(data)


class CheckedPlayback:
    """Allow only registered opaque paths. Retain a small window for quality rollback."""

    def __init__(self):
        self._streams = OrderedDict()
        self._retained = None
        self._lock = Lock()
        self._server = _LocalAudioServer(self)
        Thread(target=self._server.serve_forever, kwargs={"poll_interval": .05},
               name="music-checked-audio", daemon=True).start()

    def address(self, url, headers, *, authorization=None):
        if authorization is not None:
            if not isinstance(authorization, AuthorizedResource):
                raise DownloadTransportError("source_scope")
            url = str(authorization.check("playback", url))
            headers = dict(authorization.headers)
        else:
            url = str(_target(url))
            headers = dict(_headers(headers))
        with self._lock:
            for token, stream in self._streams.items():
                if stream.url == url and stream.headers == headers and stream.authorization is authorization:
                    self._streams.move_to_end(token)
                    break
            else:
                token = "/" + token_urlsafe(24)
                self._streams[token] = _Stream(url, headers, authorization)
                if len(self._streams) > 3:
                    stale = next(key for key in self._streams if key != self._retained)
                    self._streams.pop(stale).stop()
        return f"http://127.0.0.1:{self._server.server_port}{token}"

    def retain(self, address):
        token = "/" + address.rsplit("/", 1)[-1]
        with self._lock:
            self._retained = token if token in self._streams else None

    def attach(self, token, client):
        with self._lock:
            stream = self._streams.get(token)
            if stream is not None and stream.active():
                stream.clients.add(client)
                return stream
            return None

    def expire(self):
        with self._lock:
            for token, stream in tuple(self._streams.items()):
                if stream.authorization is not None and not stream.active():
                    self._streams.pop(token).stop()
                    if self._retained == token:
                        self._retained = None

    def detach(self, stream, client):
        with self._lock:
            stream.clients.discard(client)

    def close(self):
        with self._lock:
            for stream in self._streams.values():
                stream.stop()
            self._streams.clear()
        self._server.shutdown()
        drained = self._server.stop_clients()
        self._server.server_close()
        return drained
