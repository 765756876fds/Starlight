"""Measure the bounded switch gate on real HLS generated for this test."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import shutil
import subprocess
import threading
import time

import pytest

from app.movie.switch_probe import verify
from tests.integration.test_movie_resume_mpv import media as media, qapp as qapp, sessions as sessions
from tests.unit.test_movie_safe_switch import fake_controller, item
from app.movie.safe_switch import assess_context

pytestmark = pytest.mark.integration


def test_generated_long_hls_verification_matrix(media, tmp_path, sessions, qapp, monkeypatch):
    ffmpeg = media[0].parents[1] / "ffmpeg/ffmpeg.exe"
    assets = tmp_path / "matrix"
    assets.mkdir()

    def make(name, duration=72, segment=4, colour="black", bitrate="80k"):
        directory = assets / name
        directory.mkdir()
        subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"color=c={colour}:s=160x90:r=10:d={duration}", "-an",
                        "-c:v", "mpeg2video", "-threads", "2", "-bf", "0",
                        "-b:v", bitrate, "-g", str(int(segment*10)), "-sc_threshold", "0",
                        "-hls_time", str(segment), "-hls_playlist_type", "vod",
                        "-hls_segment_filename", str(directory/"%04d.ts"), str(directory/"vod.m3u8")],
                       check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
        return directory

    normal = make("normal")
    make("resegment", segment=6)
    make("recode", bitrate="160k")
    red = make("different_work", colour="red")
    shutil.copytree(normal, assets / "alternate_opening")
    shutil.copyfile(red / "0000.ts", assets / "alternate_opening/0000.ts")
    make("long", duration=600, segment=2)
    make("short", duration=4, segment=1)
    shutil.copytree(normal, assets / "oversize")
    with (assets / "oversize/0000.ts").open("ab") as handle:
        handle.truncate(9*1024*1024)


    # Build single-object VOD playlists from generated TS, preserving every segment.
    def pack(name, source):
        dest = assets / name
        dest.mkdir()
        lines = []
        with (dest / "video.ts").open("wb") as stream:
            for line in (source / "vod.m3u8").read_text().splitlines():
                if line.startswith("#EXT-X-VERSION:"):
                    line = "#EXT-X-VERSION:4"
                if line.endswith(".ts"):
                    data = (source / line).read_bytes()
                    lines.append(f"#EXT-X-BYTERANGE:{len(data)}@{stream.tell()}")
                    stream.write(data)
                    line = "video.ts"
                lines.append(line)
        (dest / "vod.m3u8").write_text("\n".join(lines) + "\n", encoding="utf8")

    pack("packed", assets / "long")
    for name in ("tail_changed", "range_shift", "no_range", "over_budget", "rotated"):
        shutil.copytree(assets / "packed", assets / name)
    with (assets / "tail_changed/video.ts").open("r+b") as output:
        output.seek(-1, 2)
        last = output.read(1)[0]
        output.seek(-1, 2)
        output.write(bytes([last ^ 1]))
    path = assets / "range_shift/vod.m3u8"
    path.write_text(re.sub(r"@0\n", "@188\n", path.read_text(), count=1))
    path = assets / "over_budget/vod.m3u8"
    too_large = 50000 * 188
    path.write_text("#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:2\n"
                    "#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:2,\n"
                    f"#EXT-X-BYTERANGE:{too_large}@0\nvideo.ts\n#EXTINF:2,\n"
                    f"#EXT-X-BYTERANGE:376@{too_large}\nvideo.ts\n#EXT-X-ENDLIST\n")

    requests_seen = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests_seen.append(self.path.split("/")[1])
            if self.path.endswith("/video.ts") and self.path != "/no_range/video.ts":
                bounds = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                if bounds:
                    path = assets / self.path.strip("/")
                    size = path.stat().st_size
                    start = int(bounds[1])
                    end = min(int(bounds[2]) if bounds[2] else size - 1, size - 1)
                    if not 0 <= start <= end:
                        self.send_error(416)
                        return
                    etag = '"v2"' if self.path.startswith("/rotated/") and start else '"v1"'
                    # Exercise misbehaving origins which return 206 despite a stale If-Match.
                    if start and self.headers.get("User-Agent") == "StarlightMediaProbe/1":
                        if self.headers.get("If-Match") != '"v1"':
                            self.send_error(412)
                            return
                    self.send_response(206)
                    self.send_header("ETag", etag)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.end_headers()
                    with path.open("rb") as stream:
                        stream.seek(start)
                        self.wfile.write(stream.read(end - start + 1))
                    return

            if self.path.startswith("/slow/"):
                self.path = self.path.replace("/slow/", "/short/", 1)
                if self.path.endswith(".ts"):
                    time.sleep(.85)
            try:
                super().do_GET()
            except (OSError, ConnectionError):
                pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0),
                                   functools.partial(Handler, directory=str(assets))) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()

    def address(server, name):
        return f"http://127.0.0.1:{servers[server].server_port}/{name}/vod.m3u8"

    cases = [
        ("identical_72s", "normal", "normal", "verified"),
        ("identical_short", "short", "short", "verified"),
        ("same_content_different_segments", "normal", "resegment", "timeline_mismatch"),
        ("reencoded_same_timeline", "normal", "recode", "content_mismatch"),
        ("alternate_opening", "normal", "alternate_opening", "content_mismatch"),
        ("identical_600s", "long", "long", "budget_exceeded"),
        ("long_packed", "packed", "packed", "verified"),
        ("changed_object_version", "packed", "rotated", "source_unavailable"),
        ("last_byte_differs", "packed", "tail_changed", "content_mismatch"),
        ("bad_range_layout", "packed", "range_shift", "timeline_mismatch"),
        ("server_ignores_range", "packed", "no_range", "source_unavailable"),
        ("oversized_range_objects", "over_budget", "over_budget", "budget_exceeded"),
        ("byte_budget", "normal", "oversize", "budget_exceeded"),
        ("slow_alternative", "short", "slow", "candidate_too_slow"),
        ("missing_alternative", "normal", "missing", "source_unavailable"),
    ]
    results = {}
    try:
        for name, left, right, expected in cases:
            requests_seen.clear()
            started = time.monotonic()
            result = verify(dict(original=address(0, left), candidate=address(1, right), loopback=True))
            reason = "verified" if result["status"] == "ok" else result.get("reason")
            results[name] = {"reason": reason, "bytes": result.get("bytes"),
                             "requests": len(requests_seen), "duration": result.get("duration"),
                             "elapsed_ms": round((time.monotonic()-started)*1000)}
            assert reason == expected, (name, results)
        assert results["long_packed"]["requests"] == 6
        assert results["oversized_range_objects"]["requests"] == 2
        for name, alt, expected in (
            ("no_candidate", [], "no_candidate"),
            ("same_name_different_work", [item("alt", identity="tt7654321")], "identity_insufficient"),
            ("different_episode", [item("alt", episode="第2集")], "episode_mismatch"),
        ):
            context, reason = assess_context(fake_controller(), alt)
            assert context is None and reason == expected
            results[name] = {"reason": reason}
        # Actual MPV reads both range playlists, seeks well beyond the first minute,
        # and confirms advancement. Only the admission stall bit is injected.
        from app.movie import safe_switch
        from app.movie.cms_parser import normalise_playback
        from app.movie.playback_diagnostics import read_sample
        from tests.integration.test_movie_safe_switch_mpv import wait

        urls = [address(i, "packed") for i in range(2)]
        raw = "$$$".join("正片$" + u for u in urls)
        original = normalise_playback(dict(item("owned", identity=""), vod_play_url=raw))
        session = sessions("long-range-switch")
        try:
            assert session.ctl.play(original)
            wait(qapp, lambda: read_sample(session.mpv.ipc_pipe, expected_path=urls[0]).get("time-pos") is not None)
            assert session.mpv._send_command("seek", [351, "absolute+exact"])["error"] == "success"
            before = wait(qapp, lambda: (lambda pos: pos if pos and 351 <= pos < 354 else None)(
                read_sample(session.mpv.ipc_pipe, expected_path=urls[0]).get("time-pos")))
            context, reason = assess_context(session.ctl, ())
            assert not reason and context.target == urls[1]
            def admit(pipe, timeout, *, expected_path, **options):
                sample = read_sample(pipe, timeout, expected_path=expected_path, **options)
                if expected_path == urls[0]:
                    sample["paused-for-cache"] = True
                return sample
            monkeypatch.setattr(safe_switch, "read_sample", admit)
            assert session.ctl.switch_verified_source(context, results["long_packed"]["duration"], lambda: True) == "started"
            observed = read_sample(session.mpv.ipc_pipe, expected_path=urls[1])
            assert before - .75 <= observed["time-pos"] <= before + 3
            assert session.ctl.current_item()["_playback_line_index"] == 1
            results["native_progress"] = [before, observed["time-pos"]]
        finally:
            session.ctl.close()
            session.mpv.stop_process()


    finally:
        (tmp_path/"matrix-results.json").write_text(json.dumps(results, indent=2))
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(2)
            assert not thread.is_alive()
