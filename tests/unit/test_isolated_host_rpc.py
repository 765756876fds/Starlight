import asyncio
import socket
import threading

import pytest

from app.music.online import download_probe
from app.music.online.isolated_host.session import MAX_FRAME, FrameReader, read_document


def test_rpc_frames_across_pipe_reads():
    reader = FrameReader()
    assert list(reader.feed(b'{"v":')) == []
    assert [read_document(frame) for frame in reader.feed(b'1}\n{"v":2}\n')] == [
        {"v": 1}, {"v": 2},
    ]
    assert list(reader.feed(b" " * MAX_FRAME)) == []
    with pytest.raises(ValueError, match="frame too large"):
        list(reader.feed(b" "))


@pytest.mark.parametrize("payload", (b'{"id":1,"id":2}', b'{"v":NaN}', b'{"v":1e9999}', b'[]'))
def test_rpc_document_rejects_ambiguous_values(payload):
    with pytest.raises(ValueError):
        read_document(payload)


def test_dns_resource_remains_live_until_worker_returns(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    records = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))]

    def lookup(*args):
        started.set()
        if not release.wait(4):
            raise TimeoutError("the test did not release DNS")
        return records

    monkeypatch.setattr(download_probe.socket, "getaddrinfo", lookup)

    async def verify():
        observed = []
        resolver = download_probe._PublicResolver(on_dns_worker=observed.append)
        request = asyncio.create_task(resolver.resolve("music.test", 80))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert len(observed) == 1
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert observed[0].running()
            assert not observed[0].done()
        finally:
            release.set()
        assert await asyncio.wait_for(asyncio.wrap_future(observed[0]), 2) == records

    asyncio.run(verify())
