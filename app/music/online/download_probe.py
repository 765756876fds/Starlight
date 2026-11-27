"""Bounded HTTP evidence and checked transport for user-requested music downloads."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from io import BytesIO
import ipaddress
import re
import socket
import struct
import threading
import time

import aiohttp
from multidict import CIMultiDict
from yarl import URL

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
_TRANSITION = tuple(ipaddress.ip_network(network) for network in
                    ("64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16"))


class DownloadTransportError(ValueError):
    """A non-sensitive reason code for the surrounding download worker."""

    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.status = status


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


def _target(value):
    if not isinstance(value, str) or len(value) > 16384 or any(
        ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value
    ):
        raise DownloadTransportError("invalid_url")
    try:
        # Keep the percent encoding of signed CDN paths and query strings.
        url = URL(value, encoded=True)
        host, port = url.raw_host, url.port
        if (url.scheme not in ("http", "https") or not host or not host.isascii()
                or url.raw_user is not None or url.raw_password is not None or not 0 < port <= 65535
                or "%" in host):
            raise DownloadTransportError("blocked_target")
        if aiohttp.helpers.is_ip_address(host) and not _is_allowed_ip(host):
            raise DownloadTransportError("blocked_target")
        return url.with_fragment(None)
    except DownloadTransportError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise DownloadTransportError("invalid_url") from None


class _PublicResolver(aiohttp.abc.AbstractResolver):
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
        answers = await asyncio.wrap_future(lookup)
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


def _session(timeout, buffer_size):
    connector = aiohttp.TCPConnector(resolver=_PublicResolver(), use_dns_cache=False,
                                     force_close=True, limit=1)
    return aiohttp.ClientSession(
        connector=connector,
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        auto_decompress=False,
        read_bufsize=buffer_size,
        timeout=aiohttp.ClientTimeout(total=None, sock_read=min(timeout, 20)),
    )


def _headers(headers, *, prefix=None):
    safe = CIMultiDict({"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
    for name, value in (headers or {}).items():
        if (isinstance(name, str) and name.lower() in _ORIGIN_HEADERS
                and isinstance(value, str) and len(value) <= 8192
                and not any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value)):
            safe[name] = value
    if prefix is not None:
        safe["Range"] = f"bytes=0-{prefix - 1}"
    return safe


@asynccontextmanager
async def _request(session, method, url, headers, evidence=None):
    for _ in range(5):
        if evidence is not None:
            evidence.requests += 1
        async with session.request(method, url, headers=headers, allow_redirects=False) as response:
            if response.status not in _REDIRECTS:
                yield response
                return
            location = response.headers.get("Location")
            if not location:
                raise DownloadTransportError("invalid_redirect")
            if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in location):
                raise DownloadTransportError("invalid_redirect")
            try:
                target = _target(str(url.join(URL(location, encoded=True))))
            except DownloadTransportError:
                raise
            except (ValueError, TypeError, UnicodeError):
                raise DownloadTransportError("invalid_redirect") from None
            if url.scheme == "https" and target.scheme != "https":
                raise DownloadTransportError("blocked_redirect")
            if target.origin() != url.origin():
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


async def _inspect(url, headers, timeout, max_bytes, verify_body, evidence):
    head_target = None
    async with _session(timeout, max(1024, min(max_bytes, 8192))) as session:
        async with _request(session, "HEAD", url, _headers(headers), evidence) as response:
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
        async with _request(session, "GET", url, _headers(headers, prefix=max_bytes), evidence) as response:
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
                   cancelled=None, verify_body=True):
    """Probe a single previously resolved resource on a background worker."""
    evidence = ProbeEvidence()
    if timeout <= 0 or not _PROBE_SLOTS.acquire(blocking=False):
        evidence.reason = "budget"
        return evidence
    started = time.perf_counter()
    try:
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        target = _target(url)
        cap = max(0, min(int(max_bytes), _MAX_PREFIX))
        asyncio.run(_bounded(_inspect(target, headers, timeout, cap, verify_body, evidence), timeout, cancelled))
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


async def _stream(url, headers, part, progress, timeout, expected_size):
    async with _session(timeout, 65536) as session:
        async with _request(session, "GET", url, _headers(headers)) as response:
            if response.status != 200 or not _identity_encoding(response) or "Content-Range" in response.headers:
                raise DownloadTransportError("unexpected_response", response.status)
            length = _full_length(response)
            if length is not None and expected_size is not None and length != expected_size:
                raise DownloadTransportError("size_changed")
            total = length or expected_size
            received, last_update = 0, 0.0
            with open(part, "wb") as dest:
                async for chunk in response.content.iter_chunked(65536):
                    received += len(chunk)
                    if total is not None and received > total:
                        raise DownloadTransportError("size_changed")
                    dest.write(chunk)
                    now = time.monotonic()
                    if now - last_update >= 0.2:
                        progress(min(99, int(100 * received / total)) if total else None)
                        last_update = now
            if not received or total is not None and received != total:
                raise DownloadTransportError("empty_or_incomplete")
            return received


def stream_download(url, headers, part, progress, *, timeout=180.0, cancelled=None, expected_size=None):
    """Write a validated HTTP transfer to a worker-owned temporary file."""
    if cancelled is not None and cancelled():
        raise InterruptedError("cancelled")
    target = _target(url)
    try:
        return asyncio.run(_bounded(_stream(target, headers, part, progress, timeout, expected_size),
                                    timeout, cancelled))
    except TimeoutError:
        raise DownloadTransportError("timeout") from None
    except (aiohttp.ClientError, socket.gaierror):
        raise DownloadTransportError("network") from None


async def _read_attachment(url, timeout, max_bytes):
    async with _session(timeout, min(max_bytes, 65536)) as session:
        async with _request(session, "GET", url, _headers({"User-Agent": "Starlight"})) as response:
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


def read_download_attachment(url, *, max_bytes=4 * 1024 * 1024, timeout=8.0, cancelled=None):
    """Read a small optional attachment with the same target policy as audio."""
    if cancelled is not None and cancelled():
        raise InterruptedError("cancelled")
    if type(max_bytes) is not int or not 0 < max_bytes <= 4 * 1024 * 1024:
        raise DownloadTransportError("attachment_budget")
    target = _target(url)
    try:
        return asyncio.run(_bounded(_read_attachment(target, timeout, max_bytes), timeout, cancelled))
    except TimeoutError:
        raise DownloadTransportError("timeout") from None
    except (aiohttp.ClientError, socket.gaierror):
        raise DownloadTransportError("network") from None
