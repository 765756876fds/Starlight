"""Download acceptance with generated media and the production selection path."""

from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock
import os
import shutil
import subprocess
import threading

import numpy as np
import pytest
import soundfile

from app.music.download_assets import inspect_audio
from app.music.downloader import DownloadManager
from app.music.online import download_probe
from app.music.online.manager import OnlineSourceManager
from app.music.online.models import OnlineTrack


pytestmark = pytest.mark.unit
SAMPLE_TITLE = "Generated recording"
SAMPLE_ARTIST = "Generated performer"
SAMPLE_ALBUM = "Generated release"
SAMPLE_ID = "scoped-native-id"
ISRC = "ZZABC2600001"
NO_ATTACHMENTS = {"lyrics": False, "cover": False, "metadata": False}


@pytest.fixture(scope="module")
def flac(tmp_path_factory):
    folder = tmp_path_factory.mktemp("selection-flac")
    media = {}
    for rate, depth in ((44100, 16), (96000, 24)):
        file = folder / f"generated-{depth}.flac"
        audio = np.zeros((rate, 2))
        audio[80:160, 0] = .0625
        soundfile.write(file, audio, rate, subtype=f"PCM_{depth}")
        media[depth] = file.read_bytes()
    return media


@pytest.fixture(scope="module")
def large_mp3(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    bundled = root / "third_party" / "ffmpeg" / "ffmpeg.exe"
    ffmpeg = os.environ.get("STARLIGHT_TEST_FFMPEG") or (
        str(bundled) if bundled.is_file() else shutil.which("ffmpeg"))
    if not ffmpeg:
        pytest.skip("FFmpeg is unavailable for the generated MP3 test")
    file = tmp_path_factory.mktemp("selection-mp3") / "generated.mp3"
    subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=330:sample_rate=44100:duration=1",
         "-c:a", "libmp3lame", "-b:a", "320k", str(file)],
        timeout=12, capture_output=True, check=True,
    )
    return file.read_bytes()


@pytest.fixture
def origin(monkeypatch):
    monkeypatch.setattr(download_probe, "_is_allowed_ip", lambda address: address == "127.0.0.1")
    payloads, failures, seen = {}, {}, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self):
            requested = self.headers.get("Range")
            kind = "head" if self.command == "HEAD" else "range" if requested else "full"
            seen.append((self.path, kind))
            status = failures.get((self.path, kind), 200 if self.path in payloads else 404)
            original = payloads.get(self.path, b"") if status == 200 else b""
            body = original
            if requested and status == 200:
                end = min(len(original), int(requested.split("-")[1]) + 1)
                body = original[:end]
                status = 206
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/octet-stream")
            if status == 206:
                self.send_header("Content-Range", f"bytes 0-{len(body) - 1}/{len(original)}")
            self.end_headers()
            if kind != "head" and body:
                with suppress(ConnectionError):
                    self.wfile.write(body)

        do_GET = reply
        do_HEAD = reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", payloads, failures, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


class ScopedAdapter:
    def __init__(self, name, address, paths, calls):
        self.name = name
        self.address = address
        self.paths = paths
        self.calls = calls
        self.attempts = 0

    def close(self):
        pass

    def download_tiers(self, track):
        return (("super", "super"),)

    def get_download_rendition(self, track, native, *, timeout):
        self.calls.append((self.name, track.source_name, track.track_id, native))
        assert track.source_name == self.name and track.track_id == SAMPLE_ID and native == "super"
        path = self.paths[min(self.attempts, len(self.paths) - 1)]
        self.attempts += 1
        return {"url": self.address + path, "source_quality": "super"}


@pytest.fixture
def setup(tmp_path, origin):
    address, _, _, _ = origin
    manager = OnlineSourceManager()
    calls = []
    download = DownloadManager(manager, Mock(), str(tmp_path / "downloads"))

    def owner(name, *paths, isrc=ISRC, declared_bytes=117):
        manager.register(ScopedAdapter(name, address, paths, calls))
        return OnlineTrack(name, SAMPLE_ID, SAMPLE_TITLE, SAMPLE_ARTIST, SAMPLE_ALBUM, 1,
                           extra={"_raw": {"isrc": isrc, "qualities": {"super": {
                               "format": "flac", "sizeBytes": declared_bytes,
                           }}}})

    try:
        yield download, owner, calls
    finally:
        download.close()
        download._pool.shutdown(wait=True)
        download._probe_pool.shutdown(wait=True)
        manager.close()


def song(preferred, *alternatives):
    return {"source_name": preferred.source_name, "track_id": preferred.track_id,
            "title": preferred.title, "artist": preferred.artist, "album": preferred.album,
            "duration_seconds": preferred.duration_seconds, "extra": preferred.extra,
            "_download_alternatives": tuple(alternatives)}


def run(download, selected_song, options=None, stage=None):
    complete = threading.Event()
    result = {}

    def finish(kind):
        def callback(*args):
            result[kind] = args[-1]
            complete.set()
        return callback

    callbacks = {kind: finish(kind) for kind in ("on_done", "on_fail", "on_exists", "on_cancel")}
    callbacks["on_details"] = lambda _, data: result.update(details=data)
    if stage:
        callbacks["on_stage"] = stage
    assert download.download(selected_song, callbacks, options={**NO_ATTACHMENTS, **(options or {})})
    assert complete.wait(10), result
    # Await worker cleanup before checking published and temporary files.
    download._pool.shutdown(wait=True)
    assert not list(Path(download.download_dir).glob("*.part"))
    return result


def read_menu(download, selected_song):
    complete = threading.Event()
    choices = []

    def callback(options):
        choices.extend(options)
        complete.set()

    assert download.probe_options(selected_song, callback, lambda: False)
    assert complete.wait(9)
    return choices


def test_fourth_source_wins_with_decoded_24bit_over_larger_mp3(origin, setup, flac, large_mp3):
    _, payloads, _, seen = origin
    payloads.update({"/big.mp3": large_mp3, "/one.flac": flac[16], "/two.flac": flac[16],
                     "/best.flac": flac[24], "/conflict.flac": flac[24]})
    assert len(large_mp3) > len(flac[24])
    download, owner, calls = setup
    primary = owner("first", "/big.mp3")
    one = owner("second", "/one.flac")
    two = owner("third", "/two.flac")
    best = owner("fourth", "/best.flac")
    other = owner("unsafe", "/conflict.flac", isrc="ZZABC2600002")
    result = run(download, song(primary, one, two, best, other))
    assert [entry[0] for entry in calls] == ["first", "second", "third", "fourth"]
    assert all(routed == native_owner and track_id == SAMPLE_ID for native_owner, routed, track_id, _ in calls)
    assert not [request for request in seen if request[0] == "/conflict.flac"]
    assert [path for path, phase in seen if phase == "full"] == ["/best.flac"]
    path = Path(result["on_done"])
    assert path.read_bytes() == flac[24]
    inspected = inspect_audio(path)
    assert (inspected["codec"], inspected["bits_per_sample"], inspected["sample_rate"], inspected["channels"]) == (
        "flac", 24, 96000, 2,
    )
    assert inspected["evidence"] == "decoded" and result["details"]["media_evidence"] == "decoded"


def test_menu_finds_alternate_and_preserves_declared_vs_observed_media(origin, setup, flac, large_mp3):
    _, payloads, failures, seen = origin
    payloads["/misdeclared"] = large_mp3
    payloads["/alt"] = flac[24]
    failures["/alt", "head"] = 405
    download, owner, _ = setup
    request = song(owner("primary", "/misdeclared"), owner("alternate", "/alt"))
    choices = {entry.owner: entry for entry in read_menu(download, request)}
    assert set(choices) == {"primary", "alternate"}
    mp3 = choices["primary"]
    assert (mp3.extension, mp3.codec, mp3.declared_quality, mp3.size_bytes) == (
        ".mp3", "mp3", "super", len(large_mp3),
    )
    alternate = choices["alternate"]
    assert (alternate.extension, alternate.bit_depth, alternate.sample_rate, alternate.size_bytes) == (
        ".flac", 24, 96000, len(flac[24]),
    )
    assert alternate.size_evidence == "content_range"
    assert not [phase for _, phase in seen if phase == "full"]
    assert not Path(download.download_dir).exists()
    result = run(download, request, {"format": ".flac", "representation": alternate, "allow_fallback": False})
    assert Path(result["on_done"]).read_bytes() == flac[24]
    assert result["details"]["declared_quality"] == "super"
    assert result["details"]["codec"] == "flac" and result["details"]["media_evidence"] == "decoded"


def test_explicit_flac_does_not_silently_save_a_declared_super_mp3(origin, setup, large_mp3):
    _, payloads, _, seen = origin
    payloads["/misdeclared"] = large_mp3
    download, owner, _ = setup
    result = run(download, song(owner("primary", "/misdeclared")), {"format": ".flac", "allow_fallback": False})
    assert result["on_fail"] == "所选音质或格式暂不可用"
    assert not [phase for _, phase in seen if phase == "full"]
    assert not list(Path(download.download_dir).iterdir())


@pytest.mark.parametrize(("phase", "status"), [("probe", 401), ("transfer", 403)])
def test_expired_address_refresh_uses_same_owner_and_rendition(origin, setup, flac, phase, status):
    _, payloads, failures, seen = origin
    payloads.update({"/expired": flac[24], "/fresh": flac[24]})
    for request_kind in (("head", "range", "full") if phase == "probe" else ("full",)):
        failures["/expired", request_kind] = status
    download, owner, calls = setup
    result = run(download, song(owner("primary", "/expired", "/fresh")))
    assert len(calls) == 2 and calls[0] == calls[1] == ("primary", "primary", SAMPLE_ID, "super")
    assert ("/fresh", "full") in seen
    assert Path(result["on_done"]).read_bytes() == flac[24]


def test_user_file_created_during_validated_download_wins(origin, setup, flac):
    _, payloads, _, _ = origin
    payloads["/source"] = flac[16]
    download, owner, _ = setup
    target = Path(download.download_dir) / f"{SAMPLE_TITLE} - {SAMPLE_ARTIST}.flac"

    def user_writes_before_publication(_, stage):
        if stage == "正在保存":
            target.write_bytes(b"existing user content")

    result = run(download, song(owner("primary", "/source")), stage=user_writes_before_publication)
    assert "on_exists" in result
    assert target.read_bytes() == b"existing user content"
    download.db.insert_song.assert_not_called()


def test_explicit_hires_rejects_lossy_media_even_when_auto_fallback_allowed(origin, setup, large_mp3, monkeypatch):
    monkeypatch.setattr(ScopedAdapter, "download_tiers", lambda self, track: (("hires", "super"),))
    _, payloads, _, seen = origin
    payloads["/misdeclared"] = large_mp3
    download, owner, _ = setup
    result = run(download, song(owner("primary", "/misdeclared")),
                 options={"quality": "hires", "allow_fallback": True})
    assert result["on_fail"]
    assert ("/misdeclared", "full") in seen
    assert not list(Path(download.download_dir).glob("*.mp3"))
