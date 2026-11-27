"""Verify passive media evidence and cancellation through isolated HTTP connections."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
import socket
import threading
import time

import pytest

from app.music.online import download_probe as probe


def _flac_header():
    signature = (96000 << 44) | (1 << 41) | (23 << 36) | 96000
    return (b"fLaC\x80\x00\x00\x22" + b"\x00" * 10
            + signature.to_bytes(8, "big") + b"\x00" * 16)


@pytest.fixture
def local_http(monkeypatch):
    allow = probe._is_allowed_ip
    monkeypatch.setattr(probe, "_is_allowed_ip", lambda host: host == "127.0.0.1" or allow(host))
    seen = defaultdict(list)
    entered = Queue()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def reply(self, status, *, length=None, headers=None, body=b""):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()
            if body:
                with suppress(ConnectionError, OSError):
                    self.wfile.write(body)
                    self.wfile.flush()

        def wait_for_disconnect(self):
            done = threading.Event()
            entered.put(done)
            self.connection.settimeout(2)
            try:
                if self.connection.recv(1) == b"":
                    done.set()
            except ConnectionError:
                done.set()
            except TimeoutError:
                pass
            finally:
                self.close_connection = True

        def do_HEAD(self):
            seen[self.path].append((self.command, dict(self.headers)))
            if self.path == "/slow-head":
                self.wait_for_disconnect()
                return
            if self.path in ("/range", "/bad-range", "/no-size"):
                self.reply(405, length=0)
                return
            self.reply(200, length=4 if self.path == "/changed" else 2048)

        def do_GET(self):
            seen[self.path].append((self.command, dict(self.headers)))
            path = self.path.split("?", 1)[0]
            if path in ("/redirect", "/local-redirect"):
                location = redirect_url if path == "/redirect" else "http://127.0.0.2:1/unused"
                self.reply(302, length=0, headers={"Location": location})
                return
            if path == "/expired":
                self.reply(410, length=0)
                return
            if path == "/continuous-drip":
                self.reply(200, length=8192)
                with suppress(ConnectionError, OSError):
                    for _ in range(200):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(.015)
                self.close_connection = True
                return
            if path == "/drip":
                self.reply(200, length=8192, body=b"x")
                self.wait_for_disconnect()
                return
            if path == "/truncated":
                self.reply(200, length=20, body=b"short")
                self.close_connection = True
                return
            if path == "/cover":
                # Minimal PNG header sufficient for the existing cover validator.
                body = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + (16).to_bytes(4, "big") * 2
                self.reply(200, length=len(body), body=body)
                return
            if path == "/unknown-cover-length":
                self.reply(200, headers={"Connection": "close"}, body=b"x" * 65)
                self.close_connection = True
                return
            if path == "/mp4":
                body = b"\x00\x00\x00\x14ftypM4A " + b"\x00" * 8
                self.reply(200, length=len(body), body=body)
                return
            if path in ("/range", "/bad-range", "/no-size"):
                body = _flac_header()
                total = "*" if path == "/no-size" else "17942"
                start = 1 if path == "/bad-range" else 0
                self.reply(206, length=len(body), headers={"Content-Range": f"bytes {start}-41/{total}"},
                           body=body)
                return
            # This origin ignores Range and supplies a whole representation.
            body = _flac_header() + bytes(2048 - 42)
            self.reply(200, length=len(body), body=body)

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    workers = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
               for server in servers]
    redirect_url = f"http://127.0.0.1:{servers[1].server_port}/target?signature=%2f%2B"
    for worker in workers:
        worker.start()
    try:
        yield f"http://127.0.0.1:{servers[0].server_port}", seen, entered
    finally:
        for server, worker in zip(servers, workers):
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)


def test_range_reports_full_size_only_from_content_range(local_http):
    base, seen, _ = local_http
    evidence = probe.probe_download(base + "/range", max_bytes=64)
    assert evidence.available is True
    assert evidence.size_bytes == 17942
    assert evidence.size_evidence == "content_range"
    assert (evidence.container, evidence.codec, evidence.sample_rate, evidence.bit_depth, evidence.channels) == (
        "flac", "flac", 96000, 24, 2
    )
    assert evidence.bytes_read == 42 and evidence.requests == 2
    assert seen["/range"][1][1]["Range"] == "bytes=0-63"

    unknown = probe.probe_download(base + "/no-size", max_bytes=64)
    assert unknown.available is True and unknown.size_bytes is None
    invalid = probe.probe_download(base + "/bad-range", max_bytes=64)
    assert invalid.available is None and invalid.reason == "invalid_range" and invalid.bytes_read == 0


def test_ignored_range_is_bounded_and_mp4_does_not_claim_a_codec(local_http):
    base, seen, _ = local_http
    evidence = probe.probe_download(base + "/ignored", max_bytes=64)
    assert evidence.available is True and evidence.bytes_read == 64
    assert evidence.size_bytes == 2048 and evidence.size_evidence == "response_length"
    assert evidence.requests == 2 and seen["/ignored"][1][1]["Range"] == "bytes=0-63"
    changed = probe.probe_download(base + "/changed", max_bytes=64)
    assert changed.size_conflict and changed.size_bytes == 2048
    mp4 = probe.probe_download(base + "/mp4", max_bytes=64)
    assert mp4.container == "mp4" and mp4.codec is None

    head = probe.probe_download(base + "/head", verify_body=False)
    assert head.requests == 1 and head.bytes_read == 0
    assert head.available is True and head.size_bytes == 2048 and head.codec is None


def test_expired_response_does_not_suggest_availability(local_http):
    base, _, _ = local_http
    evidence = probe.probe_download(base + "/expired", max_bytes=64)
    assert evidence.available is False and evidence.http_status == 410
    assert evidence.size_bytes is None and evidence.reason == "http_unavailable"


def test_redirect_strips_sensitive_headers_and_keeps_signed_path_encoding(local_http):
    base, seen, _ = local_http
    headers = {"Authorization": "fixture-auth", "Cookie": "test=fixture",
               "Referer": "https://example.invalid/fixture", "User-Agent": "fixture-agent"}
    evidence = probe.probe_download(base + "/redirect", headers, max_bytes=64)
    assert evidence.available is True and evidence.requests == 3
    assert seen["/redirect"][1][1]["Authorization"] == "fixture-auth"
    sent = seen["/target?signature=%2f%2B"][0][1]
    assert sent.get("User-Agent") == "fixture-agent"
    assert not {"authorization", "cookie", "referer"} & {key.lower() for key in sent}
    denied = probe.probe_download(base + "/local-redirect", max_bytes=64)
    assert denied.reason == "blocked_target" and denied.requests == 2


def test_transport_checks_full_length_and_retains_sanitized_http_status(local_http, tmp_path):
    base, seen, _ = local_http
    part = tmp_path / "download.part"
    samples = []
    assert probe.stream_download(base + "/redirect", {"Cookie": "fixture=1"}, part, samples.append,
                                 expected_size=2048) == 2048
    assert part.read_bytes().startswith(_flac_header())
    assert samples
    assert "Cookie" not in seen["/target?signature=%2f%2B"][0][1]
    with pytest.raises(probe.DownloadTransportError, match="size_changed"):
        probe.stream_download(base + "/ignored", None, part, samples.append, expected_size=2049)
    with pytest.raises(probe.DownloadTransportError, match="unexpected_response") as failure:
        probe.stream_download(base + "/expired", None, part, samples.append)
    assert failure.value.status == 410
    with pytest.raises(probe.DownloadTransportError):
        probe.stream_download(base + "/truncated", None, part, samples.append)


def test_production_address_guard_blocks_unroutable_targets_without_requests():
    for url in ("http://127.0.0.1/", "http://[::1]/", "http://[::ffff:192.168.0.1]/",
                "http://[64:ff9b::c0a8:1]/", "http://224.0.0.1/", "file:///fixture"):
        evidence = probe.probe_download(url)
        assert evidence.reason == "blocked_target" and evidence.requests == 0
    assert probe._is_allowed_ip("8.8.8.8")


def test_mixed_dns_response_is_rejected_before_any_request(local_http, monkeypatch):
    base, seen, _ = local_http
    real = socket.getaddrinfo

    def resolve(host, port, *args):
        if host == "fixture.invalid":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port)),
            ]
        return real(host, port, *args)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    evidence = probe.probe_download(base.replace("127.0.0.1", "fixture.invalid") + "/dns", max_bytes=64)
    assert evidence.reason == "blocked_target"
    assert not seen["/dns"]


def test_slow_headers_and_continuous_body_share_an_absolute_deadline(local_http):
    base, _, entered = local_http
    for path in ("/slow-head", "/drip"):
        evidence = probe.probe_download(base + path, timeout=.3)
        assert evidence.reason == "timeout" and evidence.available is None
        assert evidence.elapsed_ms < 1000
        assert entered.get(timeout=1).wait(1)
    continuous = probe.probe_download(base + "/continuous-drip", timeout=.3)
    assert continuous.reason == "timeout" and continuous.available is None
    assert 1 < continuous.bytes_read < 8192 and continuous.elapsed_ms < 1000


def test_cancelled_connections_release_on_twenty_rounds(local_http):
    base, _, entered = local_http
    with ThreadPoolExecutor(max_workers=1) as pool:
        for _ in range(20):
            cancel = threading.Event()
            result = pool.submit(probe.probe_download, base + "/drip", cancelled=cancel.is_set)
            disconnected = entered.get(timeout=1)
            cancel.set()
            evidence = result.result(timeout=1)
            assert evidence.reason == "cancelled" and evidence.available is None
            assert disconnected.wait(1)
            assert evidence.bytes_read <= 1


def test_three_in_flight_probes_hold_budget_until_connections_close(local_http):
    base, _, entered = local_http
    release = threading.Event()
    with ThreadPoolExecutor(max_workers=3) as pool:
        running = [pool.submit(probe.probe_download, base + "/slow-head",
                               cancelled=release.is_set) for _ in range(3)]
        try:
            disconnections = [entered.get(timeout=1) for _ in running]
            rejected = probe.probe_download(base + "/range")
            assert rejected.reason == "budget" and rejected.requests == 0
        finally:
            release.set()
        assert all(task.result(timeout=1).reason == "cancelled" for task in running)
        assert all(event.wait(1) for event in disconnections)
    assert probe.probe_download(base + "/range", max_bytes=64).available is True


def test_cover_uses_checked_transport_and_keeps_validation(local_http):
    from types import SimpleNamespace

    from app.music.download_assets import cover_for_track

    base, seen, _ = local_http
    data, mime, suffix = cover_for_track(SimpleNamespace(cover_url=base + "/cover"))
    assert data.startswith(b"\x89PNG") and (mime, suffix) == ("image/png", ".png")
    assert seen["/cover"][0][1]["User-Agent"] == "Starlight"
    assert cover_for_track(SimpleNamespace(cover_url=base + "/local-redirect")) is None
    assert not seen["/unused"]


@pytest.mark.parametrize("path", ["/ignored", "/unknown-cover-length"])
def test_cover_respects_size_cap_with_or_without_content_length(local_http, path):
    base, _, _ = local_http
    with pytest.raises(probe.DownloadTransportError, match="attachment_too_large"):
        probe.read_download_attachment(base + path, max_bytes=64)


def test_cancelling_cover_closes_the_connection(local_http):
    base, _, entered = local_http
    cancelled = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(probe.read_download_attachment, base + "/drip",
                             cancelled=cancelled.is_set)
        disconnected = entered.get(timeout=1)
        cancelled.set()
        with pytest.raises(InterruptedError):
            result.result(timeout=1)
        assert disconnected.wait(1)
