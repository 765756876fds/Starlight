
"""Opt-in host policy tested against owned HTTP servers and generated media."""
from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import re
from threading import Event, Thread
import wave

import pytest

from app.music.download_assets import cover_for_track
from app.music.download_journal import restore_song, song_record
from app.music.downloader import DownloadManager
from app.music.online.download_probe import (
    TransferCheckpoint, probe_download, read_download_attachment, request_plugin_http, stream_download,
)
from app.music.online.manager import OnlineSourceManager
from app.music.online.models import OnlineTrack
from app.music.online.source_authority import (
    DownloadTransportError, ResourcePolicy, SourceAuthority, TransferBudget,
)

HEADER = b"\x89PNG\r\n\x1a\n" + bytes(8) + b"\0\0\0\1\0\0\0\1"
KINDS = ("host_http", "playback", "download", "cover", "lyrics", "metadata")


@pytest.fixture
def origins():
    sample = BytesIO()
    with wave.open(sample, "wb") as sound:
        sound.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        sound.writeframes(bytes(160_000))
    wav = sample.getvalue()
    records = []
    hits = Counter()
    release_slow = Event()
    started_slow = Event()
    peer_closed = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_bytes(self, status, data, content_type, **headers):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            hits["POST"] += 1
            self.send_bytes(200, body, "application/octet-stream")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            route = self.path.partition("?")[0]
            hits[route] += 1
            records.append((route, self.command, self.headers.get("Range"), self.headers.get("X-Fixture")))
            if route in ("/allow", "/deny"):
                self.send_bytes(302, b"x" * 12, "text/plain",
                                Location=urls["allowed" if route == "/allow" else "denied"] + "/result")
            elif route == "/lyric":
                self.send_bytes(200, b"[00:00.00]fixture", "text/plain")
            elif route == "/cover":
                self.send_bytes(200, HEADER, "image/png")
            elif route in ("/audio", "/signed"):
                match = re.fullmatch(r"bytes=(\d+)-(\d+)?", self.headers.get("Range", ""))
                if match:
                    start = int(match[1])
                    end = min(len(wav) - 1, int(match[2])) if match[2] else len(wav) - 1
                    self.send_bytes(206, wav[start:end + 1], "audio/wav",
                                    ETag='"stable"', Content_Range=f"bytes {start}-{end}/{len(wav)}")
                else:
                    self.send_bytes(200, wav, "audio/wav", ETag='"stable"')
            elif route == "/slow":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "65536")
                self.end_headers()
                self.wfile.write(b"begin")
                self.wfile.flush()
                started_slow.set()
                try:
                    for _ in range(100):
                        if release_slow.wait(.03):
                            break
                        self.wfile.write(bytes(1024))
                        self.wfile.flush()
                except (ConnectionError, OSError):
                    peer_closed.set()
            else:
                self.send_bytes(200, b"abcdefghijkl", "text/plain")

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(3)]
    for server in servers:
        server.daemon_threads = True
    urls = dict(zip(("main", "allowed", "denied"), (f"http://127.0.0.1:{s.server_port}" for s in servers)))
    threads = [Thread(target=server.serve_forever, kwargs={"poll_interval": .025}, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield urls, records, hits, wav, started_slow, peer_closed
    finally:
        release_slow.set()
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(1)
            assert not thread.is_alive()


def binding(origin, *, extra_origins=(), authority=None, version=1, name="fixture", plugin="publisher-adapter"):
    root = authority or SourceAuthority()
    allowed = (origin, *extra_origins)
    budgets = {
        role: ResourcePolicy(allowed, TransferBudget(6, 65536 if role == "host_http" else 1024 * 1024),
                             frozenset({"x-fixture", "referer"}))
        for role in KINDS
    }
    return root, root.bind(name, plugin, version, budgets, test_loopback_origins=allowed)


class FixtureSource:
    name = "fixture"

    def __init__(self, urls):
        self.base = urls["main"]
        self.foreign = urls["denied"]
        self.media = self.base + "/audio"
        self.asset = self.base

    def search(self, _keyword, _limit):
        return [OnlineTrack(self.name, "shared", "Fixture", cover_url=self.foreign + "/cover",
                            extra={"_raw": {"id": "shared", "url": self.foreign + "/audio",
                                            "artwork": self.foreign + "/cover"}})]

    def get_play_url(self, _track, **_kw):
        return {"url": self.media, "headers": {"X-Fixture": "bound"}, "_checked_address": True}

    def get_download_rendition(self, track, _tier, **_kw):
        return self.get_play_url(track)

    def get_cover_url(self, _track):
        return self.asset + "/cover"

    def get_lyric(self, _track):
        return {"url": self.asset + "/lyric"}

    def close(self):
        pass


def test_actual_source_paths_and_no_unregistered_resource_requests(origins):
    urls, records, hits, wav, *_ = origins
    _, lease = binding(urls["main"])
    owner = FixtureSource(urls)
    manager = OnlineSourceManager()
    manager.register(owner, lease=lease)
    try:
        track = manager.search_all("sample")["fixture"][0]
        assert track.authorization_plugin == lease.plugin_id
        assert track.cover_url is None and track.extra == {"_raw": {"id": "shared"}}
        played = manager.get_playback_url(track, "auto")
        assert played["_authorization"].kind == "playback" and "_checked_address" not in played
        download = manager.get_download_rendition(track, None)
        grant = download["_authorization"]
        proof = probe_download(download["url"], authorization=grant)
        assert proof.available and proof.size_bytes == len(wav) and proof.container == "wav"
        assert any(row[-1] == "bound" for row in records)
        assert cover_for_track(track, manager=manager)[0] == HEADER
        assert manager.get_lyric(track) == "[00:00.00]fixture"

        before = dict(hits)
        owner.media = owner.foreign + "/audio"
        owner.asset = owner.foreign
        assert manager.get_play_url(track) is None
        assert manager.get_playback_url(track, "auto") is None
        assert manager.get_download_rendition(track, None) is None
        assert cover_for_track(track, manager=manager) is None
        assert manager.get_lyric(track) is None
        assert dict(hits) == before
        owner.media = "file:///C:/fixture.wav"
        assert manager.get_play_url(track) is None
    finally:
        manager.close()
    assert not lease.active()


def test_per_hop_policy_shared_budget_and_framework_headers(origins):
    urls, records, hits, *_ = origins
    root, lease = binding(urls["main"], extra_origins=(urls["allowed"],))
    good = lease.resource("host_http", urls["main"] + "/allow", {"X-Fixture": "private"}, resource_id="rpc")
    status, _, data = asyncio.run(request_plugin_http(good, "GET"))
    assert status == 200 and data == b"abcdefghijkl"
    assert records[-2][-1] == "private" and records[-1][-1] is None
    invalid = lease.resource("host_http", urls["main"] + "/deny", resource_id="rpc")
    before = hits["/result"]
    with pytest.raises(DownloadTransportError, match="source_origin"):
        asyncio.run(request_plugin_http(invalid, "GET"))
    assert hits["/result"] == before
    with pytest.raises(DownloadTransportError, match="source_headers"):
        lease.resource("host_http", urls["main"] + "/", {"Host": "placeholder"}, resource_id="rpc")
    renewed = root.bind("fixture", "publisher-adapter", 2, {
        "host_http": ResourcePolicy((urls["main"], urls["allowed"]), TransferBudget(4, 16)),
    }, test_loopback_origins=(urls["main"], urls["allowed"]))
    tiny = renewed.resource("host_http", urls["main"] + "/allow", resource_id="rpc")
    with pytest.raises(DownloadTransportError, match="source_budget"):
        asyncio.run(request_plugin_http(tiny, "GET"))
    with pytest.raises(DownloadTransportError, match="source_inactive"):
        asyncio.run(request_plugin_http(good, "GET"))
    assert not lease.active()


def test_running_http_is_released_after_revoke(origins):
    urls, _, _, _, started, peer_closed = origins
    root, lease = binding(urls["main"])
    grant = lease.resource("host_http", urls["main"] + "/slow", resource_id="rpc")

    async def exercise():
        task = asyncio.create_task(request_plugin_http(grant, "GET"))
        assert await asyncio.to_thread(started.wait, 2)
        root.revoke_policy(lease)
        with pytest.raises(DownloadTransportError, match="source_inactive"):
            await asyncio.wait_for(task, 2)
        assert await asyncio.to_thread(peer_closed.wait, 2)

    asyncio.run(exercise())
    with pytest.raises(DownloadTransportError, match="source_policy_version"):
        binding(urls["main"], authority=root)
    _, next_version = binding(urls["main"], authority=root, version=2)
    assert next_version.active()


def test_resume_requires_fresh_explicit_owner_and_rejects_foreign_partial(origins, tmp_path):
    urls, records, _, wav, *_ = origins
    root, old = binding(urls["main"])
    url = urls["main"] + "/signed?nonce=synthetic-test"
    scope = old.resource("download", url, resource_id="shared")
    offset = 4096
    part = tmp_path / "authorized.part"
    part.write_bytes(wav[:offset])
    checkpoint = TransferCheckpoint(offset, len(wav), '"stable"', None, scope.resume_key(url))
    original = song_record({"source_name": "fixture", "track_id": "shared", "title": "Fixture",
                            "authorization_plugin": old.plugin_id, "url": url,
                            "headers": {"Authorization": "synthetic-only"}, "_authorization": scope})
    assert "nonce" not in json.dumps(original) and "Authorization" not in json.dumps(original)
    assert original["authorization_plugin"] == old.plugin_id
    old.retire()
    _, fresh = binding(urls["main"], authority=root)
    assert scope.lease is not fresh and not scope.active()
    track = DownloadManager._preferred_track(restore_song(original))
    manager = OnlineSourceManager()
    try:
        assert manager.get_download_rendition(track, None) is None
        source = FixtureSource(urls)
        source.media = url
        manager.register(source, lease=fresh)
        resource = manager.get_download_rendition(track, None)["_authorization"]
        assert resource.lease is fresh
        assert stream_download(url, None, part, lambda _: None, resume=checkpoint,
                               open_part=lambda: part.open("r+b"), authorization=resource) == len(wav)
        assert records[-1][2] == f"bytes={offset}-"
        assert hashlib.sha256(part.read_bytes()).digest() == hashlib.sha256(wav).digest()
        _, another_owner = binding(urls["main"], authority=root, name="different")
        foreign = another_owner.resource("download", url, resource_id="shared")
        part.write_bytes(wav[:offset])
        stream_download(url, None, part, lambda _: None, resume=checkpoint,
                        open_part=lambda: part.open("r+b"), authorization=foreign)
        assert records[-1][2] is None and part.read_bytes() == wav
    finally:
        manager.close()


def test_resource_reuse_requires_correct_kind_and_origin(origins):
    urls, _, hits, *_ = origins
    _, lease = binding(urls["main"])
    attachment = lease.resource("cover", urls["main"] + "/cover", resource_id="shared")
    with pytest.raises(DownloadTransportError, match="source_scope"):
        read_download_attachment(attachment.url, authorization=attachment, kind="lyrics")
    with pytest.raises(DownloadTransportError, match="source_origin"):
        read_download_attachment(urls["denied"] + "/cover", authorization=attachment)
    assert not hits


def test_download_manager_pause_and_restart_reauthorizes_without_persisting_address(tmp_path):
    from pathlib import Path
    from unittest.mock import Mock

    from tests.unit.test_download_task_lifecycle import (
        FIRST_AUDIO, OPTIONS, PAUSE_AFTER, controlled_http, eventually, finish, song, state,
    )
    from app.music.download_assets import inspect_audio

    class SignedSource(FixtureSource):
        def __init__(self, base):
            self.base = base
            self.media = base + "/original"
            self.calls = []

        def download_tiers(self, track):
            return (("standard", "standard"),)

        def get_download_rendition(self, track, tier, **kwargs):
            assert isinstance(track, OnlineTrack) and track.track_id == "original" and tier == "standard"
            self.calls.append(track.authorization_plugin)
            return dict(self.get_play_url(track), source_quality=tier)

    with controlled_http() as (origin, address):
        request = {**song(), "authorization_plugin": "publisher-adapter", "_download_alternatives": ()}
        key = DownloadManager.task_key(request, OPTIONS)
        journal = tmp_path / "tasks.sqlite"
        folder = tmp_path / "music"
        _, first_lease = binding(address)
        first_owner = SignedSource(address)
        initial = OnlineSourceManager()
        initial.register(first_owner, lease=first_lease)
        first = DownloadManager(initial, Mock(), folder, max_workers=1, journal_path=journal)
        origin.hold.add("/original")
        try:
            assert first.download(request, options=OPTIONS)
            eventually(lambda: first.get_task(key)["bytes_written"] >= PAUSE_AFTER)
            assert first.pause(key) and state(first, key, "paused")["bytes_written"] == PAUSE_AFTER
            assert origin.closed.wait(2)
        finally:
            finish(first)
            initial.close()
        assert not first_lease.active()
        assert address.encode() not in journal.read_bytes()
        assert b"X-Fixture" not in journal.read_bytes()
        restarted = OnlineSourceManager()
        _, current_lease = binding(address)
        current_owner = SignedSource(address)
        restarted.register(current_owner, lease=current_lease)
        destination = folder / "合成录音 - Test.wav"
        destination.write_bytes(b"existing file")
        second = DownloadManager(restarted, Mock(), folder, max_workers=1, journal_path=journal)
        try:
            assert state(second, key, "paused")["bytes_written"] == PAUSE_AFTER
            assert second.resume(key)
            completed = state(second, key, "completed")
            media = Path(completed["path"])
            assert destination.read_bytes() == b"existing file"
            assert media.read_bytes() == FIRST_AUDIO and inspect_audio(media)["evidence"] == "decoded"
            requests = origin.large_requests("/original")
            resumed = next(row for row in requests if row["range"] == f"bytes={PAUSE_AFTER}-")
            assert resumed["status"] == 206 and resumed["if_range"] == f'"{hashlib.sha256(FIRST_AUDIO).hexdigest()}"'
            assert first_owner.calls and current_owner.calls and all(
                item == current_lease.plugin_id for item in current_owner.calls)
        finally:
            finish(second)
            restarted.close()


def test_post_requires_explicit_rpc_policy(origins):
    urls, _, hits, *_ = origins
    root, lease = binding(urls["main"])
    grant = lease.resource("host_http", urls["main"] + "/post", resource_id="rpc")
    with pytest.raises(DownloadTransportError, match="source_method"):
        asyncio.run(request_plugin_http(grant, "POST", body=b"fixture"))
    assert hits["POST"] == 0
    allow = root.bind("fixture", lease.plugin_id, 2, {
        "host_http": ResourcePolicy((urls["main"],), TransferBudget(4, 128), methods=frozenset({"POST"})),
    }, test_loopback_origins=(urls["main"],))
    grant = allow.resource("host_http", urls["main"] + "/post", resource_id="rpc")
    status, _, response = asyncio.run(request_plugin_http(grant, "POST", body=b"fixture"))
    assert status == 200 and response == b"fixture" and hits["POST"] == 1


def test_managed_discovery_is_closed_and_resource_identity_is_explicit(origins):
    from app.music.online.source import SourceSearchError

    urls, _, hits, *_ = origins
    _, lease = binding(urls["main"])

    class NoDiscovery(FixtureSource):
        def get_top_lists(self, *args, **kwargs):
            pytest.fail("unreviewed managed discovery invoked")
        get_top_list_detail = list_plugins = browse = get_top_lists

    manager = OnlineSourceManager()
    manager.register(NoDiscovery(urls), lease=lease)
    try:
        forged = OnlineTrack("fixture", "shared", "Fixture")
        assert manager.get_play_url(forged) is None and manager.get_download_rendition(forged, None) is None
        assert not manager.get_top_lists() and not manager.get_top_lists("fixture")
        assert manager.get_top_list_detail("fixture", "synthetic") is None
        assert not manager.discovery_sources()
        with pytest.raises(SourceSearchError):
            manager.browse("fixture", "synthetic")
    finally:
        manager.close()
    assert not hits


def test_twenty_stale_resolution_replies_are_filtered_after_managed_registration(origins):
    from concurrent.futures import ThreadPoolExecutor

    urls, _, hits, *_ = origins

    class LegacyDuringRegistration(FixtureSource):
        def __init__(self, urls):
            super().__init__(urls)
            self.entered = Event()
            self.release_reply = Event()

        def get_play_url(self, _track):
            self.entered.set()
            assert self.release_reply.wait(4)
            return {"url": self.foreign + "/audio"}

    for _ in range(20):
        legacy = LegacyDuringRegistration(urls)
        _, lease = binding(urls["main"])
        manager = OnlineSourceManager()
        manager.register(legacy)
        try:
            with ThreadPoolExecutor(max_workers=1) as worker:
                ordinary_track = OnlineTrack("fixture", "shared", "Fixture")
                result = worker.submit(manager.get_play_url, ordinary_track)
                try:
                    assert legacy.entered.wait(2)
                    manager.register(FixtureSource(urls), lease=lease)
                finally:
                    legacy.release_reply.set()
                assert result.result(timeout=2) is None
            confirmed = manager.search_all("Fixture")["fixture"][0]
            current = manager.get_play_url(confirmed)
            assert current["_authorization"].lease is lease and not hits
        finally:
            manager.close()
