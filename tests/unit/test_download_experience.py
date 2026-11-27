"""Download user contracts using real WAV bytes and an isolated HTTP server."""
import io
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest
from mutagen import File
from app.music.downloader import DownloadManager, normalize_options
from app.music.download_assets import inspect_audio, lyric_for_track, save_sidecar
from app.music.online.models import OnlineTrack

pytestmark = pytest.mark.unit


def audio(rate=8000):
    data = io.BytesIO()
    with wave.open(data, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x01\x00" * rate)
    return data.getvalue()


@pytest.fixture
def server(monkeypatch):
    from app.music.online import download_probe
    monkeypatch.setattr(download_probe, "_is_allowed_ip", lambda address: str(address) == "127.0.0.1")
    resources = {"/low": audio(), "/high": audio(16000), "/bad": b"<html>not audio</html>"}
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self._serve(head=True)

        def do_GET(self):
            self._serve()

        def _serve(self, head=False):
            data = resources.get(self.path)
            if data is None:
                self.send_error(404)
                return
            size = len(data) + (10 if self.path == "/partial" else 0)
            prefix = self.headers.get("Range")
            self.send_response(206 if prefix else 200)
            self.send_header("Content-Type", "text/html" if self.path == "/bad" else "application/octet-stream")
            if prefix:
                count = min(len(data), int(prefix.removeprefix("bytes=0-")) + 1)
                data = data[:count]
                self.send_header("Content-Range", f"bytes 0-{count-1}/{size}")
                self.send_header("Content-Length", str(count))
                resources.setdefault("_probe_reads", []).append((self.path, count))
            else:
                self.send_header("Content-Length", str(size))
            self.end_headers()
            if not head:
                if not prefix:
                    hits.append(self.path)
                try:
                    self.wfile.write(data)
                except (ConnectionResetError, BrokenPipeError):
                    pass

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", resources, hits
    http.shutdown()
    http.server_close()
    thread.join(2)


@pytest.fixture
def downloader(tmp_path):
    manager = Mock()
    manager.search_all.return_value = {}
    manager.get_lyric.return_value = "[ti:星桥]\n[ar:纸鸢]\n[00:00.00]星桥\n[00:00.50]晚风"
    dl = DownloadManager(manager, Mock(), str(tmp_path / "media"))
    yield dl
    dl.close()
    dl._pool.shutdown(wait=True)


def song(**update):
    return dict(title="星桥", artist="纸鸢", album="夜航", duration_seconds=1,
                source_name="test", track_id="chosen", **update)


def run(dl, request=None, options=None):
    event = threading.Event()
    result = {}
    def finish(kind, value):
        result[kind] = value
        event.set()
    assert dl.download(request or song(), {
        "on_done": lambda k, path: finish("done", path),
        "on_fail": lambda k, reason: finish("fail", reason),
        "on_cancel": lambda k, reason: finish("cancel", reason),
        "on_exists": lambda k: finish("exists", k),
        "on_details": lambda k, details: result.update(details=details),
    }, options=options)
    assert event.wait(5), "download did not finish"
    deadline = time.monotonic() + 2
    while dl.active_count() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not list(Path(dl.download_dir).glob("*.part"))
    return result


def test_best_compares_actual_audio_not_quality_labels(downloader, server):
    base, _, hits = server
    alt = OnlineTrack("alternate", "alt", "星桥", "纸鸢", "夜航", 1)
    downloader.online_manager.search_all.return_value = {"alternate": [alt]}
    downloader.online_manager.get_play_url.side_effect = lambda t, **k: {
        "url": base + ("/low" if t.track_id == "chosen" else "/high"), "quality": "super"}
    result = run(downloader)
    assert hits == ["/high"]  # Header screening avoids transferring the lower rendition.
    assert inspect_audio(result["done"])["sample_rate"] == 16000
    assert result["details"]["quality"] == "无损"
    assert File(result["done"])["TIT2"].text == ["星桥"]
    assert Path(result["done"]).with_suffix(".lrc").read_text("utf-8").startswith("[ti:星桥]")
    assert result["details"]["cover"] == "unavailable"
    assert downloader.db.insert_song.call_args.args[0]["format"] == "wav"


@pytest.mark.parametrize("broken", ["/bad", "/missing", "/partial"])
def test_bad_high_tier_recovers_to_valid_lower_audio(downloader, server, broken):
    base, resources, hits = server
    resources["/partial"] = audio()
    downloader.online_manager.get_play_url.side_effect = lambda t, quality: {
        "url": base + (broken if quality == "super" else "/low")}
    result = run(downloader)
    assert "done" in result
    assert hits == (["/partial", "/low"] if broken == "/partial" else ["/low"])
    tiers = [c.kwargs["quality"] for c in downloader.online_manager.get_play_url.call_args_list]
    assert tiers[0] == "super" and "high" in tiers and len(tiers) <= 5


def test_repeated_bad_url_is_bounded_and_never_published(downloader, server):
    base, _, hits = server
    downloader.online_manager.get_play_url.return_value = {"url": base + "/bad"}
    result = run(downloader)
    assert "fail" in result and hits == []  # HTML is rejected before any full transfer.
    assert downloader.online_manager.get_play_url.call_count == 4
    assert list(Path(downloader.download_dir).iterdir()) == []
    downloader.db.insert_song.assert_not_called()


@pytest.mark.parametrize("other", [
    OnlineTrack("other", "x", "星桥 (Live)", "纸鸢", "夜航", 1),
    OnlineTrack("other", "x", "星桥", "翻唱歌手", "夜航", 1),
    OnlineTrack("other", "x", "星桥", "纸鸢", "另一专辑", 1),
    OnlineTrack("other", "x", "星桥", "纸鸢", "", 0),
])
def test_selected_recording_never_swapped_for_different_or_uncertain_version(downloader, server, other):
    base, _, hits = server
    downloader.online_manager.search_all.return_value = {"other": [other]}
    downloader.online_manager.get_play_url.side_effect = lambda t, **k: None if t.track_id == "chosen" else {"url": base + "/high"}
    result = run(downloader)
    assert "fail" in result and hits == []
    assert {c.args[0].track_id for c in downloader.online_manager.get_play_url.call_args_list} == {"chosen"}


def test_custom_preferences_start_at_selected_tier_and_skip_assets(downloader, server):
    base, _, _ = server
    downloader.online_manager.get_play_url.return_value = {"url": base + "/low"}
    result = run(downloader, options=dict(quality="standard", lyrics=False, cover=False, metadata=False))
    assert downloader.online_manager.get_play_url.call_args.kwargs["quality"] == "standard"
    assert result["details"]["lyrics"] == "not_requested"
    assert File(result["done"]).tags is None
    downloader.online_manager.get_lyric.assert_not_called()


def test_bad_attachments_do_not_turn_saved_audio_into_failure(downloader, server):
    base, _, _ = server
    downloader.online_manager.get_play_url.return_value = {"url": base + "/low"}
    downloader.online_manager.get_lyric.side_effect = RuntimeError("fixture attachment failure")
    result = run(downloader)
    assert Path(result["done"]).is_file()
    assert result["details"]["lyrics"] == "unavailable"
    assert result["details"]["metadata"] == "saved"


def test_existing_audio_and_sidecars_are_preserved(downloader, server):
    base, _, hits = server
    folder = Path(downloader.download_dir)
    folder.mkdir()
    target = folder / "星桥 - 纸鸢.wav"
    target.write_bytes(b"personal existing audio")
    event = []
    assert not downloader.download(song(), {"on_exists": event.append})
    assert event == ["星桥|纸鸢"] and hits == []
    assert target.read_bytes() == b"personal existing audio"
    sidecar = target.with_suffix(".lrc")
    sidecar.write_text("my lyrics", "utf-8")
    assert save_sidecar(sidecar, b"replacement") == "preserved"
    assert sidecar.read_text("utf-8") == "my lyrics"


def test_mismatched_lyric_tags_are_not_attached(downloader):
    downloader.online_manager.get_lyric.return_value = "[ti:另一首歌]\n[00:00.00]wrong"
    assert lyric_for_track(downloader.online_manager, OnlineTrack("test", "chosen", "星桥", "纸鸢")) is None


def test_no_ambiguous_search_only_download(downloader):
    downloader.online_manager.search_all.return_value = {"a": [
        OnlineTrack("a", "1", "星桥", "纸鸢", "", 0),
        OnlineTrack("b", "2", "星桥", "纸鸢", "夜航", 1)]}
    result = run(downloader, {"title": "星桥", "artist": "纸鸢"})
    assert "fail" in result
    downloader.online_manager.get_play_url.assert_not_called()


def test_cancellation_never_publishes_audio(downloader, server):
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    downloader.online_manager.get_play_url.return_value = {"url": server[0] + "/low"}
    def stream(url, headers, part, key, cb, **kwargs):
        Path(part).write_bytes(audio())
        entered.set()
        assert release.wait(3)
        return Path(part).stat().st_size
    downloader._stream = stream
    assert downloader.download(song(), {"on_cancel": lambda *a: cancelled.set()})
    assert entered.wait(2)
    downloader.close()
    release.set()
    assert cancelled.wait(2)
    downloader._pool.shutdown(wait=True)
    assert list(Path(downloader.download_dir).iterdir()) == []
    downloader.db.insert_song.assert_not_called()


def test_options_normalize_without_truthy_strings():
    assert normalize_options({"quality": "bogus", "lyrics": "false"}) == normalize_options()


def test_publish_race_does_not_replace_a_user_file(tmp_path):
    from app.music.download_assets import publish_new
    part, target = tmp_path / "temporary.part", tmp_path / "personal.wav"
    part.write_bytes(audio())
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        publish_new(part, target)
    assert target.read_bytes() == b"original" and part.exists()


def test_matching_cover_is_saved_and_embedded(downloader, server):
    import base64
    base, resources, _ = server
    picture = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    resources["/cover"] = picture
    downloader.online_manager.get_play_url.return_value = {"url": base + "/low"}
    result = run(downloader, song(cover_url=base + "/cover"))
    assert result["details"]["cover"] == "saved"
    path = Path(result["done"])
    assert path.with_suffix(".png").read_bytes() == picture
    assert File(path).tags.getall("APIC")[0].data == picture


def test_short_preview_is_not_saved_as_full_recording(downloader, server):
    base, _, _ = server
    downloader.online_manager.get_play_url.return_value = {"url": base + "/low"}
    request = song()
    request["duration_seconds"] = 180
    result = run(downloader, request)
    assert "fail" in result
    assert list(Path(downloader.download_dir).iterdir()) == []


@pytest.mark.parametrize("length", [44, 100, 1000])
def test_matching_http_length_cannot_hide_truncated_audio(downloader, server, length):
    base, resources, _ = server
    resources["/truncated"] = audio()[:length]
    downloader.online_manager.get_play_url.side_effect = lambda t, quality: {"url": base + ("/truncated" if quality == "super" else "/low")}
    result = run(downloader)
    assert "done" in result
    assert inspect_audio(result["done"])["duration"] == 1
    tiers = [c.kwargs["quality"] for c in downloader.online_manager.get_play_url.call_args_list]
    assert tiers[:2] == ["super", "high"] and len(tiers) <= 4


def test_real_flac_keeps_format_and_writes_metadata(downloader, server):
    import numpy as np
    import soundfile as sf
    base, resources, _ = server
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(16000), 16000, format="FLAC", subtype="PCM_24")
    resources["/flac"] = buffer.getvalue()
    downloader.online_manager.get_play_url.return_value = {"url": base + "/flac"}
    result = run(downloader)
    assert Path(result["done"]).suffix == ".flac"
    assert inspect_audio(result["done"])["bits_per_sample"] == 24
    assert File(result["done"])["artist"] == ["纸鸢"]

def test_probe_reads_bounded_prefix_without_publishing_and_pins_format(downloader, server):
    base, resources, hits = server
    manager = downloader.online_manager
    manager.get_play_url.side_effect = lambda t, quality: {
        "url": base + ("/bad" if quality == "super" else "/low"), "quality": "super"}
    done = threading.Event()
    found = []
    assert downloader.probe_options(song(_download_alternatives=()),
                                    lambda options: (found.extend(options), done.set()), lambda: False)
    assert done.wait(5)
    assert len(found) == 1
    option = found[0]
    assert (option.native, option.extension, option.codec) == ("high", ".wav", "pcm")
    assert option.size_bytes == len(resources["/low"])
    assert option.size_evidence == "content_range"
    assert hits == []
    assert dict(resources["_probe_reads"]) == {"/bad": len(resources["/bad"]), "/low": 8192}
    assert not Path(downloader.download_dir).exists()
    downloader.db.insert_song.assert_not_called()
    result = run(downloader, song(_download_alternatives=()),
                 options={"quality": "high", "format": ".flac", "verified_quality": "无损"})
    assert result["fail"] == "所选音质或格式暂不可用"
    manager.search_all.assert_not_called()


def test_lossy_wav_container_does_not_get_lossless_badge(tmp_path):
    import numpy as np
    import soundfile as sf

    path = tmp_path / "lossy.wav"
    sf.write(path, np.zeros(8000), 8000, format="WAV", subtype="ULAW")
    assert inspect_audio(path)["quality"] != "无损"


def test_explicit_recording_uses_only_already_grouped_candidates(downloader, server):
    base, _, _ = server
    alternative = OnlineTrack("alternate", "alt", "星桥", "纸鸢", "夜航", 1)
    manager = downloader.online_manager
    manager.get_play_url.side_effect = lambda t, **kw: {"url": base + ("/low" if t.track_id == "chosen" else "/high")}
    result = run(downloader, song(_download_alternatives=(alternative,)))
    assert inspect_audio(result["done"])["sample_rate"] == 16000
    manager.search_all.assert_not_called()

def test_shutdown_reports_queued_task_cancellation(tmp_path):
    manager = Mock()
    downloader = DownloadManager(manager, Mock(), str(tmp_path / "media"), max_workers=1)
    entered, release, active_cancel, queued_cancel = (threading.Event() for _ in range(4))

    def resolve(*args, **kwargs):
        entered.set()
        release.wait(4)
        return {}

    manager.search_all.side_effect = resolve
    try:
        assert downloader.download(song(), {"on_cancel": lambda *_: active_cancel.set()})
        assert entered.wait(2)
        queued = dict(song(), title="另一首", track_id="other")
        assert downloader.download(queued, {"on_cancel": lambda *_: queued_cancel.set()})
        downloader.close()
        assert queued_cancel.wait(2)
        release.set()
        assert active_cancel.wait(2)
        assert not list(Path(downloader.download_dir).glob("*.part"))
    finally:
        release.set()
        downloader.close()
        downloader._pool.shutdown(wait=True)
