from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
import struct
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import pytest

from app.music.online.checked_playback import CheckedPlayback
from app.music.online.source_authority import DownloadTransportError, ResourcePolicy, SourceAuthority, TransferBudget


@contextmanager
def controlled_audio():
    observed = queue.Queue()
    size = 1024 * 1024
    prefix = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", size+36, b"WAVE", b"fmt ", 16,
                         1, 1, 16000, 32000, 2, 16, b"data", size)

    class Audio(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            result = {}
            completed = Event()
            observed.put((self.headers.get("X-Fixture"), result, completed))
            self.connection.settimeout(4)
            self.send_response(200)
            self.send_header("Content-Length", str(size + len(prefix)))
            self.end_headers()
            try:
                self.wfile.write(prefix + bytes(16384))
                result["peer_closed"] = self.connection.recv(1) == b""
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                result["peer_closed"] = True
            finally:
                completed.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Audio)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": .025}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observed
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_checked_playback_rejects_wrong_scope_and_drains_twenty_revoked_sockets():
    http = build_opener(ProxyHandler({}))
    with controlled_audio() as (origin, observed):
        authority = SourceAuthority()
        policy = ResourcePolicy((origin,), TransferBudget(5, 2 * 1024 * 1024),
                                extra_headers=frozenset({"x-fixture"}))
        remote = origin + "/audio"
        playback = CheckedPlayback()
        addresses = set()
        try:
            for number in range(20):
                lease = authority.bind("fixture", "fixture-host", 1, {"playback": policy, "cover": policy},
                                       test_loopback_origins=(origin,))
                resource = lease.resource("playback", remote, {"X-Fixture": "owned"}, resource_id="song")
                if number == 0:
                    with pytest.raises(DownloadTransportError, match="source_scope"):
                        playback.address(remote, {}, authorization={})
                    cover = lease.resource("cover", remote, resource_id="song")
                    with pytest.raises(DownloadTransportError, match="source_scope"):
                        playback.address(remote, {}, authorization=cover)
                    with pytest.raises(DownloadTransportError, match="source_scope"):
                        playback.address(remote + "?other=name", {}, authorization=resource)
                address = playback.address(remote, {"X-Fixture": "untrusted"}, authorization=resource)
                assert address not in addresses and playback.address(remote, {}, authorization=resource) == address
                addresses.add(address)
                with http.open(address, timeout=2) as response:
                    assert response.read(4) == b"RIFF"
                    header, actual, ended = observed.get(timeout=1)
                    assert header == "owned"
                    authority.revoke(lease)
                    assert ended.wait(2) and actual.get("peer_closed")
                with pytest.raises(HTTPError) as stale:
                    http.open(address, timeout=2)
                assert stale.value.code == 404
                stale.value.close()
            assert observed.empty()
        finally:
            assert playback.close()
