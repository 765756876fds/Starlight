"""Exercise the guarded resumable transfer with an isolated HTTP origin."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Event, Thread
from time import perf_counter

import pytest

from app.music.online import download_probe as probe


BODY = b"a" * 8192 + b"b" * 57344
CHANGED = b"c" * len(BODY)
PREFIX_LENGTH = 8192
ETAG = '"stable"'
MODIFIED = "Fri, 27 Nov 2026 00:00:00 GMT"


@pytest.fixture
def origin(monkeypatch):
    allow = probe._is_allowed_ip
    monkeypatch.setattr(probe, "_is_allowed_ip", lambda host: host == "127.0.0.1" or allow(host))
    requests = []
    connections = Queue()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def reply(self, status, body=b"", *, declared=None, **headers):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body) if declared is None else declared))
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.close_connection = True
            if body:
                with suppress(OSError):
                    self.wfile.write(body)
                    self.wfile.flush()

        def do_GET(self):
            requested_range = self.headers.get("Range")
            requests.append((self.path, requested_range, self.headers.get("If-Range")))
            mode = self.path.split("?")[0].split("/")[1]
            if mode == "redirect":
                self.reply(302, Location="/redirect-target?session=changed")
                return
            if mode in {"403", "404", "500"}:
                self.reply(int(mode))
                return
            validators = {"ETag": ETAG, "Last_Modified": MODIFIED}
            if not requested_range:
                if mode == "unknown":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(BODY)
                    self.close_connection = True
                elif mode in {"pause", "timeout"}:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(BODY)))
                    for name, value in validators.items():
                        self.send_header(name.replace("_", "-"), value)
                    self.end_headers()
                    disconnected = Event()
                    connections.put(disconnected)
                    try:
                        self.wfile.write(BODY[:PREFIX_LENGTH])
                        self.wfile.flush()
                        self.connection.settimeout(2)
                        if not self.connection.recv(1):
                            disconnected.set()
                    except ConnectionError:
                        disconnected.set()
                    except TimeoutError:
                        pass
                    finally:
                        self.close_connection = True
                else:
                    data = CHANGED if mode in {"changed", "416-invalid", "etag-changed"} else BODY
                    self.reply(200, data, **validators)
                return
            start = int(requested_range.removeprefix("bytes=").removesuffix("-"))
            if mode in {"ignored", "changed"}:
                if mode == "changed":
                    validators["ETag"] = '"changed"'
                self.reply(200, CHANGED if mode == "changed" else BODY, **validators)
                return
            if mode.startswith("416"):
                total = len(BODY) if mode == "416-valid" else len(BODY) + 9
                self.reply(416, Content_Range=f"bytes */{total}", **validators)
                return
            if mode == "etag-changed":
                validators["ETag"] = '"changed"'
            if mode == "modified-changed":
                validators["Last_Modified"] = "Sat, 28 Nov 2026 00:00:00 GMT"
            end = len(BODY) - 2 if mode == "bad-end" else len(BODY) - 1
            advertised_start = start + 1 if mode == "bad-start" else start
            content_range = f"bytes {advertised_start}-{end}/{len(BODY)}"
            data = BODY[start:]
            declared = len(data) + 1 if mode == "bad-length" else len(data)
            self.reply(206, data[:10] if mode == "truncated" else data, declared=declared,
                       Content_Range=content_range, **validators)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests, connections
        finally:
            server.shutdown()
            worker.join(timeout=2)


def _checkpoint(url, length=PREFIX_LENGTH):
    return probe.TransferCheckpoint(length, len(BODY), ETAG, MODIFIED, sha256(url.encode()).hexdigest())


def _transfer(url, path, resume=None, save=None):
    return probe.stream_download(url, None, path, lambda _: None, resume=resume, checkpoint=save,
                                 open_part=lambda: path.open("r+b"))


def test_pause_joins_network_and_file_then_resumes_twenty_times(origin, tmp_path, record_property):
    base, requests, connections = origin
    paused_ms = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        for iteration in range(20):
            url = f"{base}/pause/{iteration}?token=sample-%2F{iteration}"
            part = tmp_path / f"owned-{iteration}.part"
            part.touch(exist_ok=False)
            data_written, stop = Event(), Event()
            checkpoints, owners = [], []

            def opener():
                stream = part.open("r+b")
                owners.append(stream)
                return stream

            def checkpoint(value):
                checkpoints.append((value, owners[-1].closed))

            future = pool.submit(probe.stream_download, url, None, part, lambda _: data_written.set(),
                                 cancelled=stop.is_set, checkpoint=checkpoint, open_part=opener)
            disconnected = connections.get(timeout=2)
            try:
                assert data_written.wait(2)
                started = perf_counter()
            finally:
                stop.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=2)
            assert disconnected.wait(2)
            paused_ms.append((perf_counter() - started) * 1000)
            saved, closed = checkpoints[-1]
            assert closed and saved == _checkpoint(url) and part.read_bytes() == BODY[:PREFIX_LENGTH]
            assert "token=" not in repr(saved)
            resume_bytes = []
            assert probe.stream_download(url, None, part, lambda _: None, resume=saved,
                                         open_part=opener, on_bytes=lambda *args: resume_bytes.append(args)) == len(BODY)
            assert resume_bytes[0] == (PREFIX_LENGTH, len(BODY))
            assert resume_bytes[-1] == (len(BODY), len(BODY))
            assert sha256(part.read_bytes()).digest() == sha256(BODY).digest()
            assert [row[1:] for row in requests[-2:]] == [(None, None), ("bytes=8192-", ETAG)]
    record_property("server_disconnect_ms", ",".join(f"{sample:.3f}" for sample in paused_ms))
    record_property("response_body_bytes", len(BODY))
    record_property("resumed_body_bytes", len(BODY) - PREFIX_LENGTH)


@pytest.mark.parametrize("mode,expected", [
    ("ignored", BODY), ("changed", CHANGED), ("etag-changed", CHANGED), ("modified-changed", BODY),
], ids=["ignored", "changed", "new-etag", "new-date"])
def test_replaced_or_ignored_range_restarts_without_mixing(origin, tmp_path, mode, expected):
    base, requests, _ = origin
    url = f"{base}/{mode}"
    path = tmp_path / "owner.part"
    # Different prefix makes any unsafe append or retained bytes observable.
    path.write_bytes(b"x" * PREFIX_LENGTH)
    results = []
    assert _transfer(url, path, _checkpoint(url), results.append) == len(expected)
    assert path.read_bytes() == expected
    assert requests[0][1:] == ("bytes=8192-", ETAG)
    restarted = len(requests) == 2
    assert restarted == (mode in {"etag-changed", "modified-changed"})
    if restarted:
        assert requests[1][1:] == (None, None)


@pytest.mark.parametrize("mode", ["bad-start", "bad-end", "bad-length", "truncated"])
def test_wrong_or_incomplete_partial_never_finishes(origin, tmp_path, mode):
    base, requests, _ = origin
    url = f"{base}/{mode}"
    path = tmp_path / "owner.part"
    path.write_bytes(BODY[:PREFIX_LENGTH])
    with pytest.raises(probe.DownloadTransportError, match="invalid_range|network|empty_or_incomplete"):
        _transfer(url, path, _checkpoint(url))
    assert requests[-1][1:] == ("bytes=8192-", ETAG)
    assert path.read_bytes().startswith(BODY[:PREFIX_LENGTH]) and path.stat().st_size < len(BODY)
    if mode != "truncated":
        assert path.stat().st_size == PREFIX_LENGTH


@pytest.mark.parametrize("mode,expected,requests_count", [
    ("416-valid", BODY, 1), ("416-invalid", CHANGED, 2),
], ids=["complete", "restart"])
def test_416_needs_complete_length_and_strong_validator(origin, tmp_path, mode, expected, requests_count):
    base, requests, _ = origin
    url = f"{base}/{mode}"
    path = tmp_path / "owner.part"
    path.write_bytes(BODY)
    assert _transfer(url, path, _checkpoint(url, len(BODY))) == len(BODY)
    assert path.read_bytes() == expected
    assert len(requests) == requests_count and requests[0][1] == "bytes=65536-"
    if requests_count == 2:
        assert requests[1][1:] == (None, None)


@pytest.mark.parametrize("change", ["weak-etag", "url", "local-size", "redirect"])
def test_unconfirmed_checkpoint_does_not_send_range_for_other_resources(origin, tmp_path, change):
    base, requests, _ = origin
    mode = "redirect" if change == "redirect" else "whole"
    url = f"{base}/{mode}?session=new"
    path = tmp_path / "owner.part"
    path.write_bytes(BODY[:PREFIX_LENGTH - (change == "local-size")])
    saved = _checkpoint(url)
    if change == "weak-etag":
        saved = replace(saved, etag='W/"stable"')
    elif change == "url":
        saved = _checkpoint(url.replace("new", "old"))
    assert _transfer(url, path, saved) == len(BODY)
    assert path.read_bytes() == BODY
    if change == "redirect":
        assert requests[0][1:] == ("bytes=8192-", ETAG)
    else:
        assert requests[0][1:] == (None, None)
    assert requests[-1][1:] == (None, None)


@pytest.mark.parametrize("code", [403, 404, 500])
def test_http_errors_preserve_the_owned_part(origin, tmp_path, code):
    base, requests, _ = origin
    url = f"{base}/{code}"
    path = tmp_path / "owner.part"
    path.write_bytes(BODY[:PREFIX_LENGTH])
    with pytest.raises(probe.DownloadTransportError, match="unexpected_response") as error:
        _transfer(url, path, _checkpoint(url))
    assert (error.value.reason, error.value.status) == ("unexpected_response", code)
    assert len(requests) == 1
    assert path.read_bytes() == BODY[:PREFIX_LENGTH]


def test_timeout_and_unknown_size_have_truthful_checkpoints(origin, tmp_path):
    base, _, connections = origin
    path = tmp_path / "owner.part"
    path.touch()
    saved = []
    url = f"{base}/timeout"
    with pytest.raises(probe.DownloadTransportError, match="timeout"):
        probe.stream_download(url, None, path, lambda _: None, timeout=.35,
                              checkpoint=saved.append, open_part=lambda: path.open("r+b"))
    assert connections.get(timeout=2).wait(2)
    assert saved[-1] == _checkpoint(url)
    assert path.read_bytes() == BODY[:PREFIX_LENGTH]

    actual = []
    assert _transfer(f"{base}/unknown", path, save=actual.append) == len(BODY)
    assert actual[-1].total_bytes is None and actual[-1].bytes_written == len(BODY)


def test_write_error_cannot_advance_a_checkpoint(origin, tmp_path):
    base, _, _ = origin
    path = tmp_path / "owner.part"
    path.touch()
    saved = []

    class FailingWriter:
        def __enter__(self):
            self.fd = path.open("r+b")
            return self

        def __exit__(self, *_):
            self.fd.close()

        def __getattr__(self, name):
            return getattr(self.fd, name)

        def write(self, chunk):
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        probe.stream_download(f"{base}/whole", None, path, lambda _: None,
                              checkpoint=saved.append, open_part=FailingWriter)
    assert [item.bytes_written for item in saved] == [0] and path.read_bytes() == b""
