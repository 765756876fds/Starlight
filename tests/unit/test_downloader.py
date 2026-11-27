"""Download scheduling, progress and publication with a controlled HTTP origin."""
import os
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
import time
from unittest.mock import MagicMock

import pytest

from app.music.database import MusicDatabase
from app.music.downloader import DownloadManager
from app.music.online import download_probe
from app.music.online.models import OnlineTrack

pytestmark = pytest.mark.unit


@pytest.fixture
def origin(monkeypatch):
    monkeypatch.setattr(download_probe, "_is_allowed_ip", lambda ip: ip == "127.0.0.1")
    policy = dict(chunks=(b"F" * 10,), total=10, delay=0)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self):
            requests.append((self.command, self.headers.get("Range")))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if policy["total"] is not None:
                self.send_header("Content-Length", str(policy["total"]))
            self.end_headers()
            if self.command == "GET":
                with suppress(ConnectionError):
                    for chunk in policy["chunks"]:
                        time.sleep(policy["delay"])
                        self.wfile.write(chunk)

        do_HEAD = reply
        do_GET = reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture", policy, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _mk_track(title="夜曲", artist="周杰伦"):
    return OnlineTrack("酷我音乐", "t1", title, artist, duration_seconds=200)


@pytest.fixture
def harness(tmp_path):
    db = MusicDatabase(str(tmp_path / "t.db"))
    dl = DownloadManager(MagicMock(), db, str(tmp_path / "dl"), max_workers=3)
    # Only media inspection is simulated in these scheduling tests. The rest of
    # the suite exercises complete media inspection using generated recordings.
    dl._inspect_audio = lambda path: dict(extension=".m4a" if Path(path).read_bytes().startswith(b"MP4") else ".flac",
        duration=200, quality="fixture", score=(1, 128000, 44100))
    try:
        yield dl, db
    finally:
        dl.close()
        dl._pool.shutdown(wait=True)
        db.close()


def _download(dl, title="夜曲", artist="周杰伦", progress=None):
    event = Event()
    results = {}

    def complete(kind, value):
        results.update(kind=kind, value=value)
        event.set()

    dl.download({"title": title, "artist": artist}, {
        "on_done": lambda _, path: complete("done", path),
        "on_fail": lambda _, reason: complete("fail", reason),
        "on_exists": lambda k: complete("exists", k),
        "on_progress": lambda _, percent: progress.append(percent) if progress is not None else None,
    })
    return event, results


def _bind_single(dl, url):
    dl.online_manager.search_all.return_value = {"fixture": [_mk_track()]}
    dl.online_manager.get_play_url.return_value = {"url": url}


def test_concurrent_workers_limited_to_3(harness, origin):
    dl, _ = harness
    url, policy, _ = origin
    policy.update(chunks=(b"F" * 1000,) * 3, total=3000, delay=.12)
    dl.online_manager.search_all.side_effect = lambda query, **kw: {"fixture": [_mk_track(query.rsplit(" ", 1)[0], "A")]}
    dl.online_manager.get_play_url.return_value = {"url": url}
    tasks = [_download(dl, f"歌{i}", "A") for i in range(5)]
    peak = 0
    t0 = time.monotonic()
    while not all(event.is_set() for event, _ in tasks) and time.monotonic() - t0 < 6:
        peak = max(peak, dl.active_count())
        time.sleep(.015)
    assert peak == 3
    assert [state["kind"] for _, state in tasks] == ["done"] * 5


def test_quality_fallback_super_then_high(harness, origin):
    dl, _ = harness
    url, _, requests = origin
    _bind_single(dl, url)
    dl.online_manager.get_play_url.side_effect = lambda track, quality: {"url": url} if quality == "high" else None
    event, state = _download(dl)
    assert event.wait(3)
    tiers = [call.kwargs["quality"] for call in dl.online_manager.get_play_url.call_args_list]
    assert tiers[:2] == ["super", "high"] and len(tiers) <= 4
    assert state["kind"] == "done" and state["value"].endswith(".flac")
    assert requests.count(("GET", None)) == 1


def test_download_inserts_into_music_table(harness, origin):
    dl, db = harness
    _bind_single(dl, origin[0])
    event, state = _download(dl)
    assert event.wait(3) and state["kind"] == "done"
    final = state["value"]
    assert os.path.isfile(final) and not list(Path(dl.download_dir).glob("*.part"))
    row = db.get_song_by_path(final)
    assert row is not None and row["title"] == "夜曲" and row["artist"] == "周杰伦"


def test_downloaded_judgement(harness):
    dl, _ = harness
    assert dl.is_downloaded("夜曲", "周杰伦") is False
    os.makedirs(dl.download_dir, exist_ok=True)
    Path(dl.download_dir, "夜曲 - 周杰伦.mp3").touch()
    assert dl.is_downloaded("夜曲", "周杰伦") is True
    assert dl.is_downloaded("晴天", "周杰伦") is False


def test_failure_does_not_block_queue(harness, origin):
    dl, _ = harness
    dl.online_manager.search_all.side_effect = lambda query, **kw: {"fixture": [_mk_track(query.rsplit(" ", 1)[0], "A")]}
    dl.online_manager.get_play_url.side_effect = lambda track, **kw: None if track.title == "坏歌" else {"url": origin[0]}
    bad, bad_state = _download(dl, "坏歌", "A")
    good, good_state = _download(dl, "好歌", "A")
    assert bad.wait(3) and good.wait(3)
    assert bad_state["kind"] == "fail" and good_state["kind"] == "done"


def test_exists_skips_no_overwrite(harness, origin):
    dl, _ = harness
    os.makedirs(dl.download_dir, exist_ok=True)
    target = Path(dl.download_dir, "夜曲 - 周杰伦.flac")
    target.write_text("原始内容", encoding="utf8")
    _bind_single(dl, origin[0])
    event, state = _download(dl)
    assert event.is_set() and state["kind"] == "exists"
    assert target.read_text(encoding="utf8") == "原始内容"
    assert origin[2] == []
    dl.online_manager.get_play_url.assert_not_called()


def test_no_content_length_progress_none(harness, origin):
    dl, _ = harness
    url, policy, _ = origin
    policy.update(chunks=(b"MP4" * 30,), total=None)
    _bind_single(dl, url)
    progress = []
    event, state = _download(dl, progress=progress)
    assert event.wait(3)
    assert None in progress
    assert state["kind"] == "done" and state["value"].endswith(".m4a")


def test_remove_download_deletes_file(harness):
    dl, _ = harness
    os.makedirs(dl.download_dir, exist_ok=True)
    target = Path(dl.download_dir, "夜曲 - 周杰伦.flac")
    target.write_bytes(b"x")
    ok, path, err = dl.remove_download("夜曲", "周杰伦")
    assert ok and Path(path) == target
    assert not target.exists()
