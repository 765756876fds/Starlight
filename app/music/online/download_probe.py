"""Bounded HTTP evidence and checked transport for user-requested music downloads."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, replace
from email.utils import format_datetime, parsedate_to_datetime
from hashlib import sha256
from io import BytesIO
import ipaddress
import os
import re
import socket
import struct
import threading
import time

import aiohttp
from multidict import CIMultiDict
from yarl import URL

from .source_authority import AuthorizedResource, DownloadTransportError, parse_remote_url

_PROBE_SLOTS = threading.BoundedSemaphore(3)
# Windows getaddrinfo may outlive a cancelled coroutine. A DNS worker keeps
# its own slot until the actual call finishes, independently of the HTTP slot.
_DNS_SLOTS = threading.BoundedSemaphore(6)
_DNS_WORKERS = ThreadPoolExecutor(max_workers=6, thread_name_prefix="download-dns")
_MAX_PREFIX = 16384
_MAX_FILE_BYTES = (1 << 53) - 1
_REDIRECTS = {301, 302, 303, 307, 308}
_UNAVAILABLE = {401, 403, 404, 410, 451}
_PUBLIC_HEADERS = {"accept", "user-agent"}
_ORIGIN_HEADERS = _PUBLIC_HEADERS | {"referer", "cookie", "authorization", "origin"}
_RANGE = re.compile(r"bytes 0-([0-9]{1,16})/([0-9]{1,16}|\*)", re.IGNORECASE)
_TRANSFER_RANGE = re.compile(r"bytes ([0-9]{1,16})-([0-9]{1,16})/([0-9]{1,16})", re.IGNORECASE)
_UNSATISFIED_RANGE = re.compile(r"bytes \*/([0-9]{1,16})", re.IGNORECASE)
_STRONG_ETAG = re.compile(r'"[!#-~]{0,256}"')
_RESOURCE_KEY = re.compile(r"[0-9a-f]{64}")
_TRANSITION = tuple(ipaddress.ip_network(network) for network in
                    ("64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16"))


@dataclass(frozen=True)
class TransferCheckpoint:
    """Durable state for an owned part file, without the resource address."""
    bytes_written: int
    total_bytes: int | None
    etag: str | None
    last_modified: str | None
    resource_key: str


@dataclass
class ProbeEvidence:
    available: bool | None = None
    http_status: int | None = None
    size_bytes: int | None = None
    size_evidence: str | None = None
    size_conflict: bool = False
    container: str | None = None
    codec: str | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    bitrate: int | None = None
    bytes_read: int = 0
    requests: int = 0
    elapsed_ms: float = 0
    reason: str = "unknown"

    @property
    def sample_rate_hz(self):
        return self.sample_rate

    @property
    def bitrate_bps(self):
        return self.bitrate


def _is_allowed_ip(address):
    """Only globally routed unicast addresses. Reject embedded transport routes."""
    if "%" in address:
        return False
    try:
        target = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(target, ipaddress.IPv6Address):
        if target.ipv4_mapped:
            return _is_allowed_ip(str(target.ipv4_mapped))
        if target.is_site_local or any(target in network for network in _TRANSITION):
            return False
    return target.is_global and not target.is_multicast and not target.is_reserved


def _target(value, *, authorization=None):
    if authorization is not None and not isinstance(authorization, AuthorizedResource):
        raise DownloadTransportError("source_scope")
    url = parse_remote_url(value) if authorization is None else authorization.target(value)
    if aiohttp.helpers.is_ip_address(url.raw_host) and not _is_allowed_ip(url.raw_host):
        if authorization is None or not authorization.local_allowed(url):
            raise DownloadTransportError("blocked_target")
    return url


class _PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, authorization=None, *, on_dns_worker=None):
        self._authorization = authorization
        self._on_dns_worker = on_dns_worker

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        if not _DNS_SLOTS.acquire(blocking=False):
            raise DownloadTransportError("dns_busy")
        try:
            lookup = _DNS_WORKERS.submit(socket.getaddrinfo, host, port, family,
                                         socket.SOCK_STREAM, socket.IPPROTO_TCP)
        except BaseException:
            _DNS_SLOTS.release()
            raise
        lookup.add_done_callback(lambda _: _DNS_SLOTS.release())
        if self._on_dns_worker is not None:
            self._on_dns_worker(lookup)
        answers = await asyncio.wrap_future(lookup)
        if self._authorization is not None:
            self._authorization.require_active()
        if not answers or any(not _is_allowed_ip(item[4][0]) for item in answers):
            raise DownloadTransportError("blocked_target")
        # The connector uses these numeric addresses directly, keeping the
        # original URL host for HTTP Host, SNI and certificate validation.
        return [
            {"hostname": host, "host": sockaddr[0], "port": sockaddr[1],
             "family": af, "proto": proto, "flags": socket.AI_NUMERICHOST}
            for af, _, proto, _, sockaddr in answers
        ]

    async def close(self):
        pass


def _session(timeout, buffer_size, *, authorization=None, on_dns_worker=None):
    resolver = _PublicResolver(authorization, on_dns_worker=on_dns_worker)
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False,
                                     force_close=True, limit=1)
    client = aiohttp.ClientSession(
        connector=connector,
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        auto_decompress=False,
        read_bufsize=buffer_size,
        timeout=aiohttp.ClientTimeout(total=None, sock_read=min(timeout, 20)),
    )
    return client


def _headers(headers, *, prefix=None):
    safe = CIMultiDict({"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
    for name, value in (headers.items() if isinstance(headers, dict) else ()):
        if (isinstance(name, str) and name.lower() in _ORIGIN_HEADERS
                and isinstance(value, str) and len(value) <= 8192
                and not any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value)):
            safe[name] = value
    if prefix is not None:
        safe["Range"] = f"bytes=0-{prefix - 1}"
    return safe


def _resource_key(url, authorization=None):
    if authorization is not None:
        return authorization.resume_key(url)
    return sha256(str(url).encode("utf-8")).hexdigest()



class _Ledger:
    def __init__(self, authorization):
        self.authorization = authorization
        self.requests = authorization.budget.requests
        self.header_bytes = authorization.budget.header_bytes
        self.body_bytes = authorization.budget.body_bytes
        self.upload_bytes = authorization.budget.upload_bytes

    def request(self, body):
        self.requests -= 1
        self.upload_bytes -= len(body or b"")
        if self.requests < 0 or self.upload_bytes < 0:
            raise DownloadTransportError("source_budget")

    def headers(self, pairs):
        self.header_bytes -= sum(len(name) + len(value) + 4 for name, value in pairs)
        if self.header_bytes < 0:
            raise DownloadTransportError("source_budget")


class _LimitedBody:
    def __init__(self, content, ledger):
        self._content = content
        self._ledger = ledger

    async def read(self, count):
        self._ledger.authorization.require_active()
        data = await self._content.read(min(count, self._ledger.body_bytes + 1))
        if not self._ledger.authorization.active():
            raise DownloadTransportError("source_inactive")
        self._ledger.body_bytes -= len(data)
        if self._ledger.body_bytes < 0:
            raise DownloadTransportError("source_budget")
        return data

    async def readexactly(self, count):
        buffer = bytearray()
        while len(buffer) < count:
            chunk = await self.read(count - len(buffer))
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(buffer), count)
            buffer.extend(chunk)
        return bytes(buffer)

    async def iter_chunked(self, count):
        while chunk := await self.read(count):
            yield chunk


class _LimitedResponse:
    def __init__(self, response, ledger):
        self.status = response.status
        self.headers = response.headers
        self.content_type = response.content_type
        self.url = response.url
        self.content = _LimitedBody(response.content, ledger)


@asynccontextmanager
async def _request(session, method, url, headers, evidence=None, *, resume=None, authorization=None,
                   ledger=None, body=None):
    if authorization is None:
        async with _request_chain(session, method, url, headers, evidence, resume=resume,
                                  authorization=None, ledger=None, body=None) as response:
            yield response
        return
    if not authorization.method_allowed(method):
        raise DownloadTransportError("source_method")
    # The hop ledger owns retries. Reject an unsupported client rather than
    # silently allowing an internal retry outside the request count.
    if not hasattr(session, "_retry_connection"):
        raise DownloadTransportError("source_transport_version")
    session._retry_connection = False
    # The caller may only add transport-generated range fields. Origin
    # credentials and arbitrary response fields never modify the grant.
    trusted = _headers(None)
    trusted.update(authorization.headers)
    if "Range" in headers:
        trusted["Range"] = headers["Range"]
    with authorization.slot():
        async with asyncio.timeout(authorization.budget.timeout):
            async with _request_chain(session, method, url, trusted, evidence, resume=resume,
                                      authorization=authorization, ledger=ledger or _Ledger(authorization),
                                      body=body) as response:
                yield response


def _scope(authorization):
    if authorization is None:
        return {}
    return {"authorization": authorization}


def _cancel_check(cancelled, authorization):
    if authorization is None:
        return cancelled
    def check():
        if not authorization.active():
            raise DownloadTransportError("source_inactive")
        return bool(cancelled and cancelled())
    return check


def _authorized_target(url, authorization, kind):
    if authorization is None:
        return _target(url)
    if not isinstance(authorization, AuthorizedResource):
        raise DownloadTransportError("source_scope")
    authorization.check(kind, url)
    return _target(url, authorization=authorization)


@asynccontextmanager
async def _request_chain(session, method, url, headers, evidence, *, resume, authorization, ledger, body):
    for _ in range(5):
        if evidence is not None:
            evidence.requests += 1
        if authorization is not None:
            _target(str(url), authorization=authorization)
            ledger.request(body)
        sent = headers
        if resume is not None and _resource_key(url, authorization) == resume.resource_key:
            sent = headers.copy()
            sent["Range"] = f"bytes={resume.bytes_written}-"
            sent["If-Range"] = resume.etag
        payload = {"data": body} if body is not None else {}
        async with session.request(method, url, headers=sent, allow_redirects=False, **payload) as response:
            if ledger is not None:
                ledger.headers(response.raw_headers)
            if response.status not in _REDIRECTS:
                yield response if ledger is None else _LimitedResponse(response, ledger)
                return
            if ledger is not None:
                # Charge any application bytes read from intermediate responses.
                async for _ in _LimitedBody(response.content, ledger).iter_chunked(8192):
                    pass
                locations = response.headers.getall("Location", [])
                if len(locations) != 1:
                    raise DownloadTransportError("invalid_redirect")
            location = response.headers.get("Location")
            if not location:
                raise DownloadTransportError("invalid_redirect")
            if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in location):
                raise DownloadTransportError("invalid_redirect")
            try:
                destination = str(url.join(URL(location, encoded=True)))
                target = (_target(destination) if authorization is None else
                          _target(destination, authorization=authorization))
            except DownloadTransportError:
                raise
            except (ValueError, TypeError, UnicodeError):
                raise DownloadTransportError("invalid_redirect") from None
            if url.scheme == "https" and target.scheme != "https":
                raise DownloadTransportError("blocked_redirect")
            if authorization is not None and (response.status == 303 and method != "HEAD"
                    or response.status in (301, 302) and method == "POST"):
                method, body = "GET", None
                if not authorization.method_allowed(method):
                    raise DownloadTransportError("source_method")
            if target.origin() != url.origin():
                if authorization is not None and method not in ("GET", "HEAD"):
                    raise DownloadTransportError("blocked_redirect")
                # Forward only the two known public plugin fields plus the
                # transport's own Range and Accept-Encoding values.
                fields = _PUBLIC_HEADERS | {"range", "accept-encoding"}
                headers = CIMultiDict((key, value) for key, value in headers.items()
                                      if key.lower() in fields)
            url = target
    raise DownloadTransportError("redirect_limit")


def _positive_length(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        size = int(value)
        if 0 < size <= _MAX_FILE_BYTES:
            return size
    return None


def _identity_encoding(response):
    return response.headers.get("Content-Encoding", "identity").lower() == "identity"


def _full_length(response):
    if not _identity_encoding(response) or "Content-Range" in response.headers:
        return None
    return _positive_length(response.headers.get("Content-Length"))


def _record_prefix(evidence, data):
    """Parse supported prefixes with the existing Mutagen media dependency."""
    from mutagen import MutagenError
    from mutagen.flac import StreamInfo
    from mutagen.mp3 import MPEGInfo
    from mutagen.wave import WaveStreamInfo

    if len(data) >= 12 and data[4:8] == b"ftyp":
        evidence.container = "mp4"
        return
    if data.startswith(b"OggS"):
        evidence.container = "ogg"
        return
    try:
        if len(data) >= 42 and data[:4] == b"fLaC" and data[4] & 127 == 0 and data[5:8] == b"\x00\x00\x22":
            evidence.container = "flac"
            info = StreamInfo(BytesIO(data[8:42]))
            evidence.codec = "flac"
            evidence.bit_depth = info.bits_per_sample
        elif data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            evidence.container = "wav"
            info = WaveStreamInfo(BytesIO(data))
            if info.audio_format in (1, 3):
                evidence.codec = "pcm" if info.audio_format == 1 else "pcm_float"
                evidence.bit_depth = info.bits_per_sample
                evidence.bitrate = info.bitrate
        elif data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 255 and data[1] & 224 == 224):
            info = MPEGInfo(BytesIO(data))
            if info.layer != 3:
                return
            evidence.container, evidence.codec = "mp3", "mp3"
            # A sampled first frame cannot establish the whole stream's bitrate.
        else:
            return
    except (MutagenError, OSError, ValueError, EOFError, struct.error):
        return
    evidence.sample_rate = info.sample_rate
    evidence.channels = info.channels


async def _inspect(url, headers, timeout, max_bytes, verify_body, evidence, authorization=None):
    head_target = None
    scope = _scope(authorization)
    options = dict(scope, ledger=_Ledger(authorization)) if scope else {}
    async with _session(timeout, max(1024, min(max_bytes, 8192)), **scope) as session:
        async with _request(session, "HEAD", url, _headers(headers), evidence, **options) as response:
            evidence.http_status = response.status
            if response.status == 200 and response.content_type not in ("text/html", "application/json"):
                evidence.size_bytes = _full_length(response)
                if evidence.size_bytes is not None:
                    head_target = response.url
                    evidence.size_evidence = "response_head"
                    if not verify_body:
                        evidence.available, evidence.reason = True, "head"
                        return
        if max_bytes == 0:
            evidence.reason = "byte_budget"
            return
        async with _request(session, "GET", url, _headers(headers, prefix=max_bytes), evidence, **options) as response:
            evidence.http_status = response.status
            if head_target is not None and response.url != head_target:
                evidence.size_bytes = evidence.size_evidence = None
            if response.status in _UNAVAILABLE:
                evidence.available, evidence.reason = False, "http_unavailable"
                evidence.size_bytes = evidence.size_evidence = None
                return
            if response.status not in (200, 206) or not _identity_encoding(response):
                evidence.reason = "http_unknown"
                return
            if response.content_type in ("text/html", "application/json"):
                evidence.available, evidence.reason = False, "not_media"
                return
            evidence_source = "response_length"
            count = max_bytes
            if response.status == 206:
                span = _RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if span is None:
                    evidence.reason = "invalid_range"
                    return
                count = int(span[1]) + 1
                size = _positive_length(span[2])
                partial_length = _positive_length(response.headers.get("Content-Length"))
                if count > max_bytes or (span[2] != "*" and (size is None or size < count)) or (
                    partial_length is not None and partial_length != count
                ):
                    evidence.reason = "invalid_range"
                    return
                evidence_source = "content_range"
            else:
                if "Content-Range" in response.headers:
                    evidence.reason = "invalid_range"
                    return
                size = _full_length(response)
                if size is not None:
                    count = min(count, size)
            prefix = bytearray()
            while len(prefix) < count:
                chunk = await response.content.read(count - len(prefix))
                if not chunk:
                    break
                prefix.extend(chunk)
                evidence.bytes_read += len(chunk)

            if size is not None:
                evidence.size_conflict = evidence.size_bytes not in (None, size)
                evidence.size_bytes, evidence.size_evidence = size, evidence_source
            requires_count = response.status == 206 or size is not None
            evidence.available = bool(prefix) and (len(prefix) == count or not requires_count)
            evidence.reason = "prefix" if evidence.available else "empty_or_incomplete"
            if evidence.available:
                _record_prefix(evidence, bytes(prefix))


async def _bounded(operation, timeout, cancelled):
    task = asyncio.create_task(operation)
    try:
        async with asyncio.timeout(timeout):
            if cancelled is not None:
                while not task.done():
                    if cancelled():
                        raise InterruptedError("cancelled")
                    await asyncio.wait((task,), timeout=0.025)
            return await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


def probe_download(url, headers=None, *, timeout=2.0, max_bytes=8192,
                   cancelled=None, verify_body=True, authorization=None):
    """Probe a single previously resolved resource on a background worker."""
    evidence = ProbeEvidence()
    if timeout <= 0 or not _PROBE_SLOTS.acquire(blocking=False):
        evidence.reason = "budget"
        return evidence
    started = time.perf_counter()
    try:
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        target = _authorized_target(url, authorization, "download")
        if authorization is not None:
            timeout = min(timeout, authorization.budget.timeout)
        cancelled = _cancel_check(cancelled, authorization)
        cap = max(0, min(int(max_bytes), _MAX_PREFIX))
        asyncio.run(_bounded(_inspect(target, headers, timeout, cap, verify_body, evidence, authorization), timeout, cancelled))
    except (InterruptedError, TimeoutError, DownloadTransportError, aiohttp.ClientError, OSError) as exc:
        if isinstance(exc, InterruptedError):
            evidence.reason = "cancelled"
        elif isinstance(exc, TimeoutError):
            evidence.reason = "timeout"
        elif isinstance(exc, DownloadTransportError):
            evidence.reason = str(exc)
        else:
            evidence.reason = "network"
    finally:
        evidence.elapsed_ms = (time.perf_counter() - started) * 1000
        _PROBE_SLOTS.release()
    return evidence


def _single_header(response, name):
    values = response.headers.getall(name, [])
    return values[0] if len(values) == 1 else None


def _strong_etag(value):
    return value if isinstance(value, str) and _STRONG_ETAG.fullmatch(value) else None


def _last_modified(value):
    if not value or len(value) > 60:
        return None
    try:
        date = parsedate_to_datetime(value)
        return format_datetime(date, usegmt=True) if date.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _resume_from_file(resume, dest):
    if (dest is None or not isinstance(resume, TransferCheckpoint)
            or type(resume.bytes_written) is not int or not 0 < resume.bytes_written <= _MAX_FILE_BYTES
            or not _strong_etag(resume.etag) or not isinstance(resume.resource_key, str)
            or not _RESOURCE_KEY.fullmatch(resume.resource_key)
            or (resume.total_bytes is not None and (type(resume.total_bytes) is not int
                or not resume.bytes_written <= resume.total_bytes <= _MAX_FILE_BYTES))):
        return None
    return resume if dest.seek(0, os.SEEK_END) == resume.bytes_written else None


def _matches_representation(response, resume, etag, last_modified, authorization=None):
    return (resume is not None and etag == resume.etag
            and _resource_key(response.url, authorization) == resume.resource_key
            and (not resume.last_modified or not last_modified or last_modified == resume.last_modified))


def _total_for_range(response, resume, expected_size):
    span = _TRANSFER_RANGE.fullmatch(_single_header(response, "Content-Range") or "")
    if not span:
        raise DownloadTransportError("invalid_range", 206)
    start, end, total = map(int, span.groups())
    length_header = _single_header(response, "Content-Length")
    if (start != resume.bytes_written or not start <= end < total <= _MAX_FILE_BYTES
            or end != total - 1 or resume.total_bytes not in (None, total)
            or expected_size not in (None, total)
            or ("Content-Length" in response.headers and _positive_length(length_header) != total - start)):
        raise DownloadTransportError("invalid_range", 206)
    return total


async def _stream(url, headers, part, progress, timeout, expected_size,
                  resume, checkpoint, open_part, on_bytes, authorization=None):
    state = durable_state = last_checkpoint = last_sample = None

    def emit_bytes():
        nonlocal last_sample
        if on_bytes is not None and state is not None:
            sample = (state.bytes_written, state.total_bytes)
            if sample != last_sample:
                on_bytes(*sample)
                last_sample = sample

    def flush(dest):
        nonlocal durable_state
        if checkpoint is not None and state != durable_state:
            dest.flush()
            os.fsync(dest.fileno())
            durable_state = state

    def persist():
        nonlocal last_checkpoint
        if checkpoint is not None and durable_state != last_checkpoint:
            checkpoint(durable_state)
            last_checkpoint = durable_state

    try:
        with ExitStack() as files:
            dest = files.enter_context(open_part()) if open_part is not None else None
            pending = _resume_from_file(resume, dest)
            scope = _scope(authorization)
            options = dict(scope, ledger=_Ledger(authorization)) if scope else {}
            async with _session(timeout, 65536, **scope) as session:
                # One retry without Range handles unconfirmed representations
                # and 416. Both requests (including redirects) share one deadline.
                for _ in range(2):
                    async with _request(session, "GET", url, _headers(headers), resume=pending, **options) as response:
                        etag = _strong_etag(_single_header(response, "ETag"))
                        modified = _last_modified(_single_header(response, "Last-Modified"))
                        same = _matches_representation(response, pending, etag, modified, authorization)
                        if response.status == 416 and pending is not None:
                            span = _UNSATISFIED_RANGE.fullmatch(_single_header(response, "Content-Range") or "")
                            total = _positive_length(span[1]) if span else None
                            if (same and total == pending.bytes_written and pending.total_bytes in (None, total)
                                    and expected_size in (None, total)):
                                state = durable_state = replace(pending, total_bytes=total)
                                return total
                            pending = None
                            continue
                        if response.status == 206 and pending is not None and not same:
                            pending = None
                            continue
                        if not _identity_encoding(response) or response.status not in (200, 206):
                            raise DownloadTransportError("unexpected_response", response.status)
                        if response.content_type in ("text/html", "application/json", "multipart/byteranges"):
                            raise DownloadTransportError("unexpected_response", response.status)
                        if response.status == 206:
                            if not same:
                                raise DownloadTransportError("invalid_range", 206)
                            total = _total_for_range(response, pending, expected_size)
                            received = pending.bytes_written
                            dest.seek(received)
                        else:
                            if "Content-Range" in response.headers:
                                raise DownloadTransportError("unexpected_response", 200)
                            length = _full_length(response)
                            if length is not None and expected_size not in (None, length):
                                raise DownloadTransportError("size_changed")
                            total, received = length or expected_size, 0
                            if dest is None:
                                dest = files.enter_context(open(part, "wb"))
                            else:
                                dest.seek(0)
                                dest.truncate()
                        state = TransferCheckpoint(received, total, etag, modified, _resource_key(response.url, authorization))
                        if not received:
                            flush(dest)
                            persist()
                        emit_bytes()
                        last_progress, last_sync = 0.0, time.monotonic()
                        try:
                            async for chunk in response.content.iter_chunked(65536):
                                if received + len(chunk) > (total or _MAX_FILE_BYTES):
                                    raise DownloadTransportError("size_changed")
                                try:
                                    if dest.write(chunk) != len(chunk):
                                        raise OSError("short part file write")
                                except OSError:
                                    state = None
                                    raise
                                received += len(chunk)
                                state = replace(state, bytes_written=received)
                                now = time.monotonic()
                                if now - last_progress >= .2:
                                    progress(min(99, int(100 * received / total)) if total else None)
                                    emit_bytes()
                                    last_progress = now
                                if now - last_sync >= 1:
                                    flush(dest)
                                    persist()
                                    last_sync = now
                        finally:
                            if state is not None:
                                flush(dest)
                        if not received or total is not None and received != total:
                            raise DownloadTransportError("empty_or_incomplete")
                        return received
    finally:
        # Notify the coordinator only after the request and file contexts exit.
        persist()
        emit_bytes()
    raise DownloadTransportError("unexpected_response")


def stream_download(url, headers, part, progress, *, timeout=180.0, cancelled=None,
                    expected_size=None, resume=None, checkpoint=None, open_part=None, on_bytes=None,
                    authorization=None):
    """Transfer a fresh or conditional resource into a worker-owned part file."""
    if cancelled is not None and cancelled():
        raise InterruptedError("cancelled")
    if resume is not None and open_part is None:
        raise ValueError("resume requires an owned file opener")
    target = _authorized_target(url, authorization, "download")
    if authorization is not None:
        timeout = min(timeout, authorization.budget.timeout)
    cancelled = _cancel_check(cancelled, authorization)
    try:
        pending = _stream(target, headers, part, progress, timeout, expected_size,
                          resume, checkpoint, open_part, on_bytes, authorization)
        return asyncio.run(_bounded(pending, timeout, cancelled))
    except TimeoutError:
        raise DownloadTransportError("timeout") from None
    except (aiohttp.ClientError, socket.gaierror):
        raise DownloadTransportError("network") from None


async def _read_attachment(url, timeout, max_bytes, authorization=None):
    scope = _scope(authorization)
    async with _session(timeout, min(max_bytes, 65536), **scope) as session:
        async with _request(session, "GET", url, _headers({"User-Agent": "Starlight"}), **scope) as response:
            if response.status != 200 or not _identity_encoding(response) or "Content-Range" in response.headers:
                raise DownloadTransportError("unexpected_response", response.status)
            length = _full_length(response)
            if length is not None and length > max_bytes:
                raise DownloadTransportError("attachment_too_large")
            body = bytearray()
            # One extra application byte distinguishes a valid limit-sized
            # attachment from an unknown-length response exceeding the cap.
            while len(body) <= max_bytes:
                chunk = await response.content.read(min(65536, max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > max_bytes:
                raise DownloadTransportError("attachment_too_large")
            if not body or length is not None and len(body) != length:
                raise DownloadTransportError("empty_or_incomplete")
            return bytes(body)


def read_download_attachment(url, *, max_bytes=4 * 1024 * 1024, timeout=8.0, cancelled=None,
                             authorization=None, kind="cover"):
    """Read a small optional attachment with the same target policy as audio."""
    if cancelled is not None and cancelled():
        raise InterruptedError("cancelled")
    if type(max_bytes) is not int or not 0 < max_bytes <= 4 * 1024 * 1024:
        raise DownloadTransportError("attachment_budget")
    if kind not in ("cover", "lyrics", "metadata"):
        raise DownloadTransportError("source_scope")
    target = _authorized_target(url, authorization, kind)
    if authorization is not None:
        timeout = min(timeout, authorization.budget.timeout)
        max_bytes = min(max_bytes, authorization.budget.body_bytes)
    cancelled = _cancel_check(cancelled, authorization)
    try:
        return asyncio.run(_bounded(_read_attachment(target, timeout, max_bytes, authorization), timeout, cancelled))
    except TimeoutError:
        raise DownloadTransportError("timeout") from None
    except (aiohttp.ClientError, socket.gaierror):
        raise DownloadTransportError("network") from None


async def request_plugin_http(authorization, method, *, body=None, cancelled=None, on_dns_worker=None):
    """Async entry point for an opt-in host RPC, with bounded response bytes."""
    if not isinstance(authorization, AuthorizedResource):
        raise DownloadTransportError("source_scope")
    url = _authorized_target(authorization.url, authorization, "host_http")
    if (body is not None and (not isinstance(body, bytes) or method != "POST")
            or authorization.budget.body_bytes > 64 * 1024):
        raise DownloadTransportError("source_budget")
    timeout = authorization.budget.timeout

    async def perform():
        async with _session(timeout, 8192, authorization=authorization,
                            on_dns_worker=on_dns_worker) as session:
            async with _request(session, method, url, _headers(None), authorization=authorization,
                                body=body) as response:
                data = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    data.extend(chunk)
                return response.status, response.content_type, bytes(data)

    return await _bounded(perform(), timeout, _cancel_check(cancelled, authorization))
