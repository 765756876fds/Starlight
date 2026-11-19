"""Measure the unchanged switch gate on real HLS generated for this test."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import subprocess
import threading
import time

import pytest

from app.movie.switch_probe import verify
from tests.integration.test_movie_resume_mpv import media as media
from tests.unit.test_movie_safe_switch import fake_controller, item
from app.movie.safe_switch import assess_context

pytestmark = pytest.mark.integration


def test_generated_long_hls_verification_matrix(media, tmp_path):
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

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
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
        ("byte_budget", "normal", "oversize", "budget_exceeded"),
        ("slow_alternative", "short", "slow", "candidate_too_slow"),
        ("missing_alternative", "normal", "missing", "source_unavailable"),
    ]
    results = {}
    try:
        for name, left, right, expected in cases:
            result = verify(dict(original=address(0, left), candidate=address(1, right), loopback=True))
            reason = "verified" if result["status"] == "ok" else result.get("reason")
            results[name] = {"reason": reason, "bytes": result.get("bytes")}
            assert reason == expected, (name, results)
        for name, alt, expected in (
            ("no_candidate", [], "no_candidate"),
            ("same_name_different_work", [item("alt", identity="tt7654321")], "identity_insufficient"),
            ("different_episode", [item("alt", episode="第2集")], "episode_mismatch"),
        ):
            context, reason = assess_context(fake_controller(), alt)
            assert context is None and reason == expected
            results[name] = {"reason": reason}
    finally:
        (tmp_path/"matrix-results.json").write_text(json.dumps(results, indent=2))
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(2)
            assert not thread.is_alive()
