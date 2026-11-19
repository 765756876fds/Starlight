"""Proof for a narrow HLS profile with an identical encoded timeline."""
import hashlib
import math
import ipaddress
import socket
import re
import time
from urllib.parse import urljoin, urlsplit

import urllib3

PROBE_MS = 9000
MAX_BYTES = 8 * 1024 * 1024  # Across both origins, including both manifests.
MAX_PACKED_SEGMENTS = 512  # A single TS object; still subject to the shared byte/time limits.
MAX_SEGMENTS = 24
MANIFEST_BYTES = 32 * 1024


class ProbeRejected(ValueError):
    """Enumerated diagnostics safe to return from the probe worker."""


def origin(url):
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.fragment or len(url) > 2048):
        raise ValueError("unsupported media URL")
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def playlist(body, url, *, restart=False):
    lines = [line.strip() for line in body.decode("utf-8-sig").splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or lines[-1] != "#EXT-X-ENDLIST":
        raise ProbeRejected("unsupported_hls")
    segments, duration = [], None
    size = None
    next_offset = 0
    packed = False
    version = None
    for line in lines[1:-1]:
        if line.startswith("#EXTINF:"):
            if duration is not None:
                raise ProbeRejected("timeline_mismatch")
            duration = float(line[8:].partition(",")[0])
            if not math.isfinite(duration) or not 0 < duration <= 20:
                raise ProbeRejected("timeline_mismatch")
        elif line.startswith("#EXT-X-BYTERANGE:"):
            bounds = re.fullmatch(r"#EXT-X-BYTERANGE:([0-9]{1,10})@([0-9]{1,10})", line)
            if not bounds or duration is None or size is not None:
                raise ProbeRejected("unsupported_hls")
            size, offset = map(int, bounds.groups())
            if offset != next_offset or size < 376 or size % 188 or (segments and not packed):
                raise ProbeRejected("timeline_mismatch")
            packed = True
        elif line.startswith("#EXT-X-VERSION:"):
            if version is not None or not re.fullmatch(r"#EXT-X-VERSION:[0-9]{1,3}", line):
                raise ProbeRejected("unsupported_hls")
            version = int(line.split(":")[1])
        elif line in ("#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"):
            continue
        elif re.fullmatch(r"#EXT-X-(?:TARGETDURATION|MEDIA-SEQUENCE):[0-9]+", line):
            continue
        elif line.startswith("#"):
            raise ProbeRejected("unsupported_hls")
        else:
            target = urljoin(url, line)
            if (duration is None or origin(target) != origin(url)
                    or not urlsplit(target).path.lower().endswith(".ts")):
                raise ProbeRejected("unsupported_hls")
            if packed:
                if size is None or (segments and target != segments[0][1]):
                    raise ProbeRejected("timeline_mismatch")
                next_offset += size
            segments.append((duration, target, size))
            size = None
            duration = None
    if packed and (version is None or version < 4 or "#EXT-X-PLAYLIST-TYPE:VOD" not in lines):
        raise ProbeRejected("unsupported_hls")
    if len(segments) > (MAX_PACKED_SEGMENTS if packed or restart else MAX_SEGMENTS):
        raise ProbeRejected("budget_exceeded")
    if duration is not None or len(segments) < 2:
        raise ProbeRejected("unsupported_hls")
    return segments


def _common_presentation(body, master, left, right):
    """Only a plain publisher variant index, with both exact CMS media URIs."""
    lines = [line.strip() for line in body.decode("utf-8-sig").splitlines() if line.strip()]
    if not lines or lines.pop(0) != "#EXTM3U":
        return False
    variants = set()
    pending = False
    for line in lines:
        if pending:
            if line.startswith("#"):
                return False
            variants.add(urljoin(master, line))
            pending = False
        elif re.fullmatch(r"#EXT-X-STREAM-INF:BANDWIDTH=[1-9][0-9]{0,9}"
                          r'(?:,(?:RESOLUTION=[0-9]+x[0-9]+|CODECS="[A-Za-z0-9., -]+"))*', line):
            pending = True
        elif not (re.fullmatch(r"#EXT-X-VERSION:[0-9]{1,3}", line) or line == "#EXT-X-INDEPENDENT-SEGMENTS"):
            return False
    return not pending and variants == {left, right}


def verify(payload):
    """One bounded worker. Return only evidence, never URLs or media bodies."""
    deadline = time.monotonic() + PROBE_MS / 1000
    remaining = MAX_BYTES
    connections = {}
    validators = {}
    loopback = payload.get("loopback") is True
    left, right = payload["original"], payload["candidate"]

    master_hint = None
    manifests_seen = {}

    def fetch(url, limit, byte_range=None):
        nonlocal remaining, master_hint
        left_s = deadline - time.monotonic()
        if left_s <= 0 or remaining <= 0:
            raise ProbeRejected("budget_exceeded")
        limit = min(limit, remaining)
        started = time.monotonic()
        scheme, host, port = origin(url)
        identity = (scheme, host, port)
        pool = connections.get(identity)
        if pool is None:
            addresses = [ipaddress.ip_address(addr[4][0])
                         for addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
            if not addresses or not all(ip.is_global or (loopback and ip.is_loopback) for ip in addresses):
                raise ProbeRejected("unsupported_origin")
            ip = str(addresses[0])  # Numeric destination pins the DNS check to the actual connection.
            pool = (urllib3.HTTPSConnectionPool(ip, port, cert_reqs="CERT_REQUIRED",
                                                assert_hostname=host, server_hostname=host)
                    if scheme == "https" else urllib3.HTTPConnectionPool(ip, port))
            connections[identity] = pool
        parts = urlsplit(url)
        resource = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        left_s = deadline - time.monotonic()
        if left_s <= 0:
            raise ProbeRejected("budget_exceeded")
        headers = {"Accept-Encoding": "identity", "Host": parts.netloc, "User-Agent": "StarlightMediaProbe/1"}
        if byte_range is not None:
            start, end, total = byte_range
            headers["Range"] = f"bytes={start}-{end}"
            if start:
                headers["If-Match"] = validators[url]
        with pool.urlopen("GET", resource, redirect=False, retries=False, preload_content=False,
                          timeout=urllib3.Timeout(connect=min(left_s, 1.5), read=min(left_s, 1.5)),
                          headers=headers) as response:
            if response.status != (206 if byte_range else 200) or response.headers.get("Content-Encoding", "identity") != "identity":
                raise ProbeRejected("source_unavailable")
            if url == left and payload.get("same_record") is True:
                # Trust only an index declared by the original publisher, on its own origin.
                up = re.fullmatch(r'<([^<>\s]{1,2048})>;\s*rel="up"', response.headers.get("Link", ""))
                if up:
                    master_hint = urljoin(left, up[1])
            length = response.headers.get("Content-Length", "")
            if not length.isdecimal() or int(length) <= 0:
                raise ProbeRejected("source_unavailable")
            if byte_range is not None and (int(length) != end - start + 1
                    or response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}"):
                raise ProbeRejected("source_unavailable")
            if byte_range is not None:
                etag = response.headers.get("ETag", "")
                if not re.fullmatch(r'"[!#-~]{1,128}"', etag) or (start and etag != validators[url]):
                    raise ProbeRejected("source_unavailable")
                validators[url] = etag
            if int(length) > limit:
                raise ProbeRejected("budget_exceeded")
            data = bytearray()
            expected = int(length)
            while len(data) < expected:
                if time.monotonic() >= deadline:
                    raise ProbeRejected("budget_exceeded")
                chunk = response.read1(min(32768, expected - len(data)))
                if not chunk:
                    raise ProbeRejected("source_unavailable")
                remaining -= len(chunk)
                data.extend(chunk)
        return data, time.monotonic() - started

    def manifest(url):
        if url not in manifests_seen:
            manifests_seen[url] = fetch(url, MANIFEST_BYTES)[0]
        return manifests_seen[url]

    def exact():
        manifests = [playlist(manifest(url), url) for url in (left, right)]
        timeline = [(duration, size) for duration, _, size in manifests[0]]
        if timeline != [(duration, size) for duration, _, size in manifests[1]]:
            raise ProbeRejected("timeline_mismatch")
        durations = [duration for duration, _ in timeline]
        if timeline[0][1] is not None:
            # Compare two contiguous ranges covering each entire object. The first
            # segment is requested separately to exercise real random access.
            sizes = [size for _, size in timeline]
            total = sum(sizes)
            if 2 * total > remaining:
                raise ProbeRejected("budget_exceeded")
            original, candidate = (segments[0][1] for segments in manifests)
            for start, group, times in ((0, sizes[:1], durations[:1]),
                                        (sizes[0], sizes[1:], durations[1:])):
                limit = sum(group)
                bounds = (start, start + limit - 1, total)
                data, elapsed = fetch(candidate, limit, bounds)
                # Conservative: even the coalesced read must fit the shortest
                # segment window. Compare all bytes, including the last segment.
                if elapsed >= min(times) * .7:
                    raise ProbeRejected("candidate_too_slow")
                offset = 0
                for size in group:
                    if data[offset] != 0x47 or data[offset+188] != 0x47:
                        raise ProbeRejected("unsupported_hls")
                    offset += size
                if data != fetch(original, limit, bounds)[0]:
                    raise ProbeRejected("content_mismatch")
        else:
            for (duration, original, _), (_, candidate, _) in zip(*manifests):
                data, elapsed = fetch(candidate, MAX_BYTES)
                if elapsed >= duration * .7:
                    raise ProbeRejected("candidate_too_slow")
                if len(data) < 376 or data[0] != 0x47 or data[188] != 0x47:
                    raise ProbeRejected("unsupported_hls")
                if hashlib.sha256(data).digest() != hashlib.sha256(fetch(original, MAX_BYTES)[0]).digest():
                    raise ProbeRejected("content_mismatch")
        if time.monotonic() >= deadline:
            raise ProbeRejected("budget_exceeded")
        return dict(status="ok", items=[], duration=sum(durations), bytes=MAX_BYTES-remaining)
    def restart():
        master = payload.get("record_master")
        if not master and left not in manifests_seen:
            manifest(left)
        master = master or master_hint
        if (not master or master in (left, right) or origin(master) != origin(left)
                or not urlsplit(master).path.lower().endswith(".m3u8")
                or not _common_presentation(manifest(master), master, left, right)):
            raise ProbeRejected("identity_insufficient")
        segments = playlist(manifest(right), right, restart=True)
        duration, first, size = segments[0]
        if size is not None:
            raise ProbeRejected("unsupported_hls")
        data, elapsed = fetch(first, 256 * 1024)
        if (len(data) < 3 * 188 or len(data) % 188 or any(data[i] != 0x47 for i in range(0, len(data), 188))
                or not any(data[i+1] & 0x1f == 0 and data[i+2] == 0 for i in range(0, len(data), 188))):
            raise ProbeRejected("unsupported_hls")
        if elapsed >= duration or time.monotonic() >= deadline:
            raise ProbeRejected("candidate_too_slow")
        return dict(status="restart_available", items=[], duration=sum(row[0] for row in segments),
                    bytes=MAX_BYTES-remaining)

    try:
        if origin(left) == origin(right):
            raise ProbeRejected("unsupported_origin")
        eligible = (payload.get("same_record") is True and
                    (loopback or all(origin(url)[0] == "https" for url in (left, right))))
        if payload.get("startup_error") is True:
            if not eligible:
                raise ProbeRejected("identity_insufficient")
            return restart()
        try:
            return exact()
        except ProbeRejected as failure:
            if not eligible or failure.args[0] not in ("content_mismatch", "timeline_mismatch", "budget_exceeded"):
                raise
            return restart()
    except ProbeRejected as failure:
        return dict(status="failed", items=[], error="UnverifiedMedia", reason=failure.args[0],
                    bytes=MAX_BYTES-remaining)
    except (ValueError, OSError, urllib3.exceptions.HTTPError):
        return dict(status="failed", items=[], error="UnverifiedMedia", reason="source_unavailable",
                    bytes=MAX_BYTES-remaining)
    finally:
        for pool in connections.values():
            pool.close()
