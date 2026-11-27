"""MusicFree and LX protocol checks using authored plugins and generated PCM."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock
import wave

import pytest

from app.music import download_assets
from app.music.downloader import DownloadManager
from app.music.online import download_probe
from app.music.online.checked_playback import CheckedPlayback
from app.music.online.content_choices import group_tracks, same_recording
from app.music.online.manager import OnlineSourceManager
from app.music.online.models import OnlineTrack
from app.music.online.sources.js_source import JSMusicSource

OPTIONS = {"lyrics": False, "cover": False, "metadata": False}


def generated_audio():
    target = BytesIO()
    with wave.open(target, "wb") as file:
        file.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        file.writeframes(b"\x0a\x00" * 16000)
    return target.getvalue()


@pytest.fixture
def origin(monkeypatch):
    # The only network exception exists in this fixture, for its loopback peers.
    monkeypatch.setattr(download_probe, "_is_allowed_ip", lambda address: address == "127.0.0.1")
    payload, requests = generated_audio(), []
    target = ""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.reply()

        def do_GET(self):
            self.reply()

        def reply(self):
            path = self.path.partition("?")[0]
            requests.append((self.server.server_port, self.command, path, dict(self.headers.items())))
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", target + "/media")
                self.end_headers()
                return
            if path in {"/missing", "/expired"}:
                self.send_response(410)
                self.end_headers()
                return
            if path == "/playlist":
                self.send_response(200)
                self.send_header("Content-Length", "7")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(b"#EXTM3U")
                return
            if path == "/need-headers" and self.headers.get("Referer") != "https://source.invalid/":
                self.send_response(403)
                self.end_headers()
                return
            span = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
            start, end = (0, len(payload) - 1)
            if span:
                start = int(span[1])
                if span[2]:
                    end = min(end, int(span[2]))
            data = payload[start:end+1]
            self.send_response(206 if span else 200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "audio/wav")
            self.send_header("ETag", '"fixture-audio"')
            self.send_header("Accept-Ranges", "bytes")
            if span:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

    with ExitStack() as stack:
        peers = [stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) for _ in range(2)]
        endpoints = [f"http://127.0.0.1:{peer.server_port}" for peer in peers]
        target = endpoints[1]
        for peer in peers:
            worker = Thread(target=peer.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
            worker.start()
            stack.callback(worker.join, 2)
            stack.callback(peer.shutdown)
        yield endpoints[0], endpoints[1], payload, requests


@pytest.fixture
def sources(tmp_path, monkeypatch, origin):
    node = os.environ.get("NODE_EXE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the actual bridge")
    monkeypatch.setenv("NODE_EXE", node)
    mf, lx = tmp_path / "mf", tmp_path / "lx"
    mf.mkdir()
    lx.mkdir()
    base = json.dumps(origin[0])
    (mf / "direct.js").write_text("""
      const base = BASE;
      const values = [
        {duration: 220.5}, {duration: 1000}, {duration: 1001}, {duration: 1300}, {duration: 1700},
        {durationMs: 125050}, {duration_ms: 87500}, {duration: true}, {duration: [1300]},
        {duration: -5}, {duration: '1300'}, {duration: Infinity}, {interval: 1300},
        {durationMs: 1000, duration_ms: 2000}, {duration: 200, durationMs: 2000}
      ];
      module.exports = {platform:'direct', async search(term, page) {
        const items = term === 'duration' ? values :
          [{duration:term === 'long' ? 1300 : 2, url:term === 'unsafe' ? 'file:///audio.wav' :
            base + (term === 'missing' ? '/missing' : '/media') + '?signature=fixture-only'}];
        return {isEnd:true, data:items.map((item, index) =>
          ({id: String(index), title:term, artist:'Artist', album:'Album', ...item}))};
      }};
    """.replace("BASE", base), encoding="utf8")
    (mf / "getter.js").write_text("""
      const base = BASE;
      module.exports = {platform:'getter', async search(term) {
        return {data:[{id:'0', title:term, url:base+'/media'}],isEnd:true};
      }, async getMediaSource(item, tier) {
        if (tier === 'high') return {url:base+'/need-headers',quality:'high'};
        return null;
      }};
    """.replace("BASE", base), encoding="utf8")
    (lx / "fixture-lx.js").write_text("""
      const base = BASE;
      const lx = globalThis.lx;
      let requests = 0;
      void lx.send(lx.EVENT_NAMES.inited, {sources:{kw:{actions:['musicUrl'],qualitys:['320k']}}});
      lx.on(lx.EVENT_NAMES.request, async ({action,source,info}) => {
        if (action !== 'musicUrl' || source !== 'kw' || info.type !== '320k') return null;
        const id = info.musicInfo.hash;
        if (id === 'plain') return base + '/media';
        return {
          url: base + (id === 'redirect' ? '/redirect' : id === 'refresh' && ++requests === 1 ?
            '/expired' : '/need-headers'),
          quality: '320k',
          headers: {'Referer':'https://source.invalid/', 'Authorization':'fixture-authorization',
            'Cookie':'fixture=1', 'User-Agent':'fixture-client', 'Host':'wrong.invalid',
            'Origin':'bad\r\nvalue', 'Range':'bytes=212-'}
        };
      });
    """.replace("BASE", base).replace("bad\r\nvalue", "bad\\r\\nvalue"), encoding="utf8")
    bridge = JSMusicSource(str(mf), str(lx))
    manager = OnlineSourceManager()
    try:
        assert bridge.available
        manager.register(bridge)
        yield manager, bridge
    finally:
        manager.close()
        bridge.close()


def item(bridge, source, term):
    return bridge.search_page(bridge._proc, source, term, 1).tracks[0]


def as_song(track):
    return dict(source_name=track.source_name, track_id=track.track_id,
                title=track.title, artist=track.artist, album=track.album,
                duration_seconds=track.duration_seconds, extra=track.extra,
                _download_alternatives=())


def lx_item(native):
    return OnlineTrack("fixture-lx", native, f"fixture {native}", "Artist", "Album", 2,
                       extra={"_raw":{"musicInfo":{"hash":native, "source":"kw"}}})


def await_download(manager, song, options=None):
    event = Event()
    result = {}
    assert manager.download(song, {
        "on_done": lambda _, path: (result.update(path=path), event.set()),
        "on_fail": lambda _, reason: (result.update(failure=reason), event.set()),
    }, options=dict(OPTIONS, **(options or {})))
    assert event.wait(7)
    return result


def test_duration_uses_protocol_seconds_and_explicit_ms_without_merging(sources):
    _, bridge = sources
    page = bridge.search_page(bridge._proc, "direct", "duration", 1)
    assert [track.duration_seconds for track in page.tracks] == [
        220.5, 1000, 1001, 1300, 1700, 125.05, 87.5, 0, 0, 0, 0, 0, 0, 0, 200
    ]
    long_a, same_title_other_recording = page.tracks[3:5]
    same_title_other_recording.source_name = "distinct source"
    assert not same_recording(long_a, same_title_other_recording)
    assert len(group_tracks([long_a, same_title_other_recording])) == 2


def test_default_direct_address_and_getter_remain_distinct(sources):
    manager, bridge = sources
    direct = item(bridge, "direct", "ready")
    getter = item(bridge, "getter", "ready")
    assert manager.download_tiers(direct) == ((None,None),)
    assert manager.get_download_rendition(direct, "high") is None
    assert manager.get_download_rendition(getter, "super") is None
    result = manager.get_download_rendition(direct, None)
    assert result["source_quality"] is None and result["requested_quality"] is None
    assert result["url"].endswith("signature=fixture-only")
    play = manager.get_playback_url(direct, "auto")
    assert play["direct_url"] and play["supports_renditions"] is False
    assert manager.get_playback_url(item(bridge,"direct","unsafe"), "auto") is None
    assert manager.get_download_rendition(item(bridge,"direct","unsafe"), None) is None
    direct.track_id = "wrong"
    assert manager.get_download_rendition(direct, None) is None


def test_real_direct_media_enters_menu_and_download_but_not_exact_tier(sources, origin, tmp_path):
    manager, bridge = sources
    direct = item(bridge, "direct", "ready")
    song = as_song(direct)
    dl = DownloadManager(manager, Mock(), str(tmp_path/"music"))
    try:
        options = Event()
        choices = []
        assert dl.probe_options(song, lambda found: (choices.extend(found), options.set()), lambda: False)
        assert options.wait(5)
        assert len(choices)==1 and choices[0].native is None and choices[0].tier is None
        assert choices[0].declared_quality is None and choices[0].size_bytes == len(origin[2])
        assert "failure" in await_download(dl, song, {"quality":"hires"})
        result = await_download(dl, song, {"representation":choices[0]})
        path = Path(result["path"])
        assert path.read_bytes() == origin[2]
        assert download_assets.inspect_audio(path)["evidence"] == "decoded"
    finally:
        dl.close()
        assert dl.wait_closed(3)


def test_direct_failure_and_restart_do_not_persist_address(sources, origin, tmp_path):
    manager, bridge = sources
    root = tmp_path/"music"
    journal = tmp_path/"tasks.sqlite3"
    song = as_song(item(bridge, "direct", "missing"))
    task = DownloadManager(manager, Mock(), str(root), journal_path=journal)
    try:
        assert "failure" in await_download(task,song)
    finally:
        task.close()
        assert task.wait_closed(3)
    assert b"signature=" not in journal.read_bytes()
    assert b"fixture-only" not in journal.read_bytes()
    restarted = DownloadManager(manager, Mock(), str(root), journal_path=journal)
    try:
        before = len(origin[3])
        end, result = Event(), []
        assert restarted.retry(restarted.task_key(song), {"on_fail":lambda *a: (result.append(a),end.set())})
        assert end.wait(5) and result
        assert len(origin[3]) == before
        assert not list(root.glob("*.wav"))
    finally:
        restarted.close()
        assert restarted.wait_closed(3)


def test_long_track_cannot_publish_a_short_preview(sources, tmp_path):
    manager, bridge = sources
    dl = DownloadManager(manager, Mock(), str(tmp_path/"music"))
    try:
        assert "failure" in await_download(dl, as_song(item(bridge,"direct","long")))
        assert not list((tmp_path/"music").glob("*.wav"))
    finally:
        dl.close()
        assert dl.wait_closed(3)


def test_lx_refresh_forwards_safe_headers_to_head_range_and_actual_file(sources, origin, tmp_path):
    manager, _ = sources
    song = as_song(lx_item("refresh"))
    dl = DownloadManager(manager,Mock(),str(tmp_path/"lx-media"))
    try:
        result = await_download(dl,song,{"quality":"high"})
        assert Path(result["path"]).read_bytes() == origin[2]
        assert download_assets.inspect_audio(result["path"])["codec"] == "pcm"
        records = [record for record in origin[3] if record[2] in ("/need-headers","/expired")]
        good = [record for record in records if record[2]=="/need-headers"]
        assert any(record[2]=="/expired" for record in records)
        assert {verb for _,verb,_,_ in good} == {"HEAD","GET"}
        assert {headers.get("Range") for _,_,_,headers in good} == {None,"bytes=0-8191"}
        for *_, headers in good:
            assert headers["Referer"] == "https://source.invalid/"
            assert headers["Authorization"] == "fixture-authorization"
            assert headers["Cookie"] == "fixture=1"
            assert headers["User-Agent"] == "fixture-client"
            assert headers["Host"].startswith("127.0.0.1:")
            assert "Origin" not in headers
    finally:
        dl.close()
        assert dl.wait_closed(3)


def test_lx_playback_guarded_media_strips_headers_on_cross_origin_redirect(sources, origin):
    manager, _ = sources
    result = manager.get_playback_url(lx_item("redirect"), "auto")
    assert result["checked_transport"]
    checked = CheckedPlayback()
    try:
        url = checked.address(result["url"],result["headers"])
        with urlopen(url,timeout=4) as response:
            assert response.read() == origin[2]
        with urlopen(Request(url,headers={"Range":"bytes=44-55"}),timeout=4) as response:
            assert response.status == 206
            assert response.read() == origin[2][44:56]
        first = [record for record in origin[3] if record[2]=="/redirect"]
        later = [record for record in origin[3] if record[2]=="/media"]
        assert first and later and all(record[0] != later[0][0] for record in first)
        assert all("Authorization" in record[3] and "Cookie" in record[3] for record in first)
        for *_,headers in later:
            assert not {"Authorization","Cookie","Referer"} & headers.keys()
            assert headers["User-Agent"] == "fixture-client"
        plain = manager.get_playback_url(lx_item("plain"), "auto")
        assert plain["url"].endswith("/media") and not plain.get("checked_transport")
    finally:
        checked.close()


def test_checked_playback_pins_current_path_during_failed_quality_attempts(origin):
    base, _, audio, requests = origin
    checked = CheckedPlayback()
    try:
        current = checked.address(base + "/media", None)
        checked.retain(current)
        with urlopen(current, timeout=4) as response:
            assert response.read() == audio
        for attempt in range(4):
            unused = checked.address(base + f"/playlist?case={attempt}", None)
            with pytest.raises(HTTPError) as error:
                urlopen(unused, timeout=4)
            assert error.value.code == 502
        with urlopen(Request(current, headers={"Range": "bytes=0-1"}), timeout=4) as response:
            assert response.status == 206 and response.read() == audio[:2]
        assert any(packet[3].get("Range") == "bytes=0-1" for packet in requests)
    finally:
        assert checked.close()
