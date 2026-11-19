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
MAX_SEGMENTS = 24
MANIFEST_BYTES = 32 * 1024


def origin(url):
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.fragment or len(url) > 2048):
        raise ValueError("unsupported media URL")
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def playlist(body, url):
    lines = [line.strip() for line in body.decode("utf-8-sig").splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or lines[-1] != "#EXT-X-ENDLIST":
        raise ValueError("finite VOD required")
    segments, duration = [], None
    for line in lines[1:-1]:
        if line.startswith("#EXTINF:"):
            if duration is not None:
                raise ValueError("missing segment")
            duration = float(line[8:].partition(",")[0])
            if not math.isfinite(duration) or not 0 < duration <= 20:
                raise ValueError("invalid duration")
        elif line in ("#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"):
            continue
        elif re.fullmatch(r"#EXT-X-(?:VERSION|TARGETDURATION|MEDIA-SEQUENCE):[0-9]+", line):
            continue
        elif line.startswith("#"):
            raise ValueError("unsupported tag")
        else:
            target = urljoin(url, line)
            if (duration is None or origin(target) != origin(url)
                    or not urlsplit(target).path.lower().endswith(".ts")):
                raise ValueError("unsupported segment")
            segments.append((duration, target))
            duration = None
    if duration is not None or not 2 <= len(segments) <= MAX_SEGMENTS:
        raise ValueError("segment count")
    return segments


def verify(payload):
    """One bounded worker. Return only evidence, never URLs or media bodies."""
    deadline = time.monotonic() + PROBE_MS / 1000
    remaining = MAX_BYTES
    connections = {}
    loopback = payload.get("loopback") is True
    left, right = payload["original"], payload["candidate"]

    def fetch(url, limit):
        nonlocal remaining
        left_s = deadline - time.monotonic()
        if left_s <= 0 or remaining <= 0:
            raise ValueError("budget")
        limit = min(limit, remaining)
        started = time.monotonic()
        scheme, host, port = origin(url)
        identity = (scheme, host, port)
        pool = connections.get(identity)
        if pool is None:
            addresses = [ipaddress.ip_address(addr[4][0])
                         for addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
            if not addresses or not all(ip.is_global or (loopback and ip.is_loopback) for ip in addresses):
                raise ValueError("nonpublic media origin")
            ip = str(addresses[0])  # Numeric destination pins the DNS check to the actual connection.
            pool = (urllib3.HTTPSConnectionPool(ip, port, cert_reqs="CERT_REQUIRED",
                                                assert_hostname=host, server_hostname=host)
                    if scheme == "https" else urllib3.HTTPConnectionPool(ip, port))
            connections[identity] = pool
        parts = urlsplit(url)
        resource = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        left_s = deadline - time.monotonic()
        if left_s <= 0:
            raise ValueError("deadline")
        with pool.urlopen("GET", resource, redirect=False, retries=False, preload_content=False,
                          timeout=urllib3.Timeout(connect=min(left_s, 1.5), read=min(left_s, 1.5)),
                          headers={"Accept-Encoding": "identity", "Host": parts.netloc,
                                   "User-Agent": "StarlightMediaProbe/1"}) as response:
            if response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("unsupported response")
            length = response.headers.get("Content-Length", "")
            if not length.isdecimal() or not 0 < int(length) <= limit:
                raise ValueError("unbounded response")
            data = bytearray()
            expected = int(length)
            while len(data) < expected:
                if time.monotonic() >= deadline:
                    raise ValueError("deadline")
                chunk = response.read1(min(32768, expected - len(data)))
                if not chunk:
                    raise ValueError("truncated segment")
                remaining -= len(chunk)
                data.extend(chunk)
        return data, time.monotonic() - started

    try:
        if origin(left) == origin(right):
            raise ValueError("same origin")
        manifests = [playlist(fetch(url, MANIFEST_BYTES)[0], url) for url in (left, right)]
        durations = [d for d, _ in manifests[0]]
        if durations != [d for d, _ in manifests[1]]:
            raise ValueError("different timeline")
        for (duration, original), (_, candidate) in zip(*manifests):
            # Segment deadline headroom is relative to the encoded time, not a Mbps threshold.
            data, elapsed = fetch(candidate, MAX_BYTES)
            if elapsed >= duration * .7 or len(data) < 376 or data[0] != 0x47 or data[188] != 0x47:
                raise ValueError("candidate transfer or format")
            if hashlib.sha256(data).digest() != hashlib.sha256(fetch(original, MAX_BYTES)[0]).digest():
                raise ValueError("different encoded media")
        if time.monotonic() >= deadline:
            raise ValueError("deadline")
        return dict(status="ok", items=[], duration=sum(durations), bytes=MAX_BYTES-remaining)
    except (ValueError, OSError, urllib3.exceptions.HTTPError):
        return dict(status="failed", items=[], error="UnverifiedMedia", bytes=MAX_BYTES-remaining)
    finally:
        for pool in connections.values():
            pool.close()
