"""M1 reproducible lab: generated HLS, localhost throttling, owned MPV only.

Example: .venv/Scripts/python.exe -m tools.movie_playback_lab --output D:/m1-new
No production config, user library, source plugin or existing IPC is opened.
"""
from __future__ import annotations

import argparse
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

import psutil
from PySide6.QtCore import QCoreApplication, QTimer

from app.movie.playback_diagnostics import PlaybackSummary, read_sample


def generate(ffmpeg, root):
    root.mkdir(parents=True, exist_ok=False)
    for name, rate, size in (("low", "350k", "320x180"), ("high", "2400k", "960x540")):
        folder = root / name
        folder.mkdir()
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=s={size}:r=25:d=60",
            "-an", "-c:v", "mpeg2video", "-threads", "2", "-bf", "0",
            "-b:v", rate, "-minrate", rate, "-maxrate", rate, "-bufsize", rate,
            "-g", "50",
            "-sc_threshold", "0", "-f", "hls", "-hls_time", "2",
            "-hls_list_size", "0", "-hls_segment_filename", str(folder / "%03d.ts"),
            str(folder / "index.m3u8"),
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (root / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=450000,RESOLUTION=320x180\n"
        "low/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2600000,RESOLUTION=960x540\n"
        "high/index.m3u8\n", encoding="utf-8",
    )
    return {name: sum(p.stat().st_size for p in (root / name).glob("*.ts")) * 8 / 60
            for name in ("low", "high")}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        self.rate_lock = threading.Lock()
        self.next_send = 0.
        super().__init__(*args, **kwargs)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.server.break_segments and self.path.endswith(".ts"):
            self.server.failures += 1
            self.send_error(503)
            return
        super().do_GET()

    def copyfile(self, source, outputfile):
        if not self.path.endswith(".ts"):
            return super().copyfile(source, outputfile)
        try:
            while not self.server.cancel.is_set():
                chunk = source.read(4096)
                if not chunk:
                    break
                # One shared byte budget across ALL segment connections.
                # Per-connection limits can accidentally multiply HLS throughput.
                with self.server.rate_lock:
                    now = time.monotonic()
                    when = max(now, self.server.next_send)
                    self.server.next_send = when + len(chunk) / self.server.bytes_per_second
                delay = when - now
                if delay > 0 and self.server.cancel.wait(delay):
                    return
                outputfile.write(chunk)
                self.server.transferred += len(chunk)
        except (ConnectionError, OSError):
            pass


def run_case(exe, root, server, name, cap=None, wait=2, seconds=22):
    from app.movie.movie_controller import _mpv_buffer_args
    from app.music.mpv import MpvController

    config = {"pause_wait": wait}
    if cap is not None:
        config["hls_bitrate"] = cap
    class Config:
        def get(self, key, default=None):
            return config if key == "movie.mpv_buffer" else default
    directory = root / name
    directory.mkdir()
    mpv = MpvController(
        str(exe), r"\\.\pipe\movie_m1_" + uuid.uuid4().hex, video=True,
        extra_video_args=[
            "--config-dir=" + str(directory), "--load-scripts=no",
            "--vo=null", "--ao=null", "--hwdec=no", "--tls-verify=yes",
            *_mpv_buffer_args(Config()),
        ],
    )
    samples, before_bytes, before_fail = [], server.transferred, server.failures
    try:
        assert mpv.start(), "owned MPV launch failed"
        process = psutil.Process(mpv._process.pid)
        cpu_start = sum(process.cpu_times()[:2])
        process.cpu_percent()
        begin = time.monotonic()
        response = mpv.loadfile(f"http://127.0.0.1:{server.server_port}/master.m3u8")
        accepted = isinstance(response, dict) and response.get("error") == "success"
        summary = PlaybackSummary(begin)
        positions, cpu, rss, latency = [], [], [], []
        while time.monotonic() - begin < seconds:
            start = time.monotonic()
            sample = read_sample(mpv.ipc_pipe)
            latency.append(time.monotonic() - start)
            result = summary.add(sample, time.monotonic())
            samples.append(result)
            if isinstance(sample.get("time-pos"), (int, float)):
                positions.append(sample["time-pos"])
            cpu.append(process.cpu_percent())
            rss.append(process.memory_info().rss)
            time.sleep(.2)
        # Exercises ordinary pause/seek IPC after observation, not part of the comparison.
        pause = mpv.set_property("pause", True)
        seek = mpv._send_command("seek", [2, "absolute"])
        resume = mpv.set_property("pause", False)
        return {
            "name": name, "cap_bps": cap, "cache_pause_wait_s": wait,
            "accepted": accepted, "observed_s": time.monotonic() - begin,
            "first_progress_s": summary.first_progress,
            "position_max_s": max(positions, default=None),
            "rebuffer_events": summary.buffer_events, "cache_wait_s": summary.buffer_seconds,
            "rebuffer_s": summary.rebuffer_seconds,
            "samples": samples, "sample_ms_max": max(latency) * 1000,
            "cpu_one_core_percent_mean": (
                (sum(process.cpu_times()[:2]) - cpu_start) / (time.monotonic() - begin) * 100
            ),
            "rss_peak_bytes": max(rss),
            "transferred_bytes": server.transferred - before_bytes,
            "segment_http_503": server.failures - before_fail,
            "pause_seek_resume_accepted": all(
                isinstance(r, dict) and r.get("error") == "success" for r in (pause, seek, resume)
            ),
        }
    finally:
        mpv.stop_process()
        assert not mpv.is_running(), "owned MPV survived cleanup"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=22, choices=range(15, 46))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from app.core.logger import get_logger
    get_logger(log_dir=str(args.output / "logs"))
    repo = Path(__file__).resolve().parents[1]
    app = QCoreApplication.instance() or QCoreApplication([])
    ticks, failures, results = [], [], []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(25)

    def work():
        server = None
        try:
            rates = generate(repo / "third_party/ffmpeg/ffmpeg.exe", args.output / "hls")
            for name, cap, wait in (
                ("original", None, 2), ("capped", 700000, 2),
                ("longer_initial_wait", None, 6), ("broken_segments", 700000, 2),
            ):
                server = Server(("127.0.0.1", 0), functools.partial(
                    Handler, directory=str(args.output / "hls")))
                server.bytes_per_second = 125000
                server.cancel = threading.Event()
                server.break_segments = name == "broken_segments"
                server.transferred = server.failures = 0
                http_thread = threading.Thread(target=server.serve_forever, daemon=True)
                http_thread.start()
                try:
                    result = run_case(repo / "third_party/mpv/mpv.exe", args.output,
                                      server, name, cap, wait,
                                      15 if server.break_segments else args.seconds)
                finally:
                    server.cancel.set()
                    server.shutdown()
                    server.server_close()
                    http_thread.join(2)
                    server = None
                results.append(result)
                (args.output / (name + ".json")).write_text(
                    json.dumps(result, indent=2), encoding="utf-8")
            (args.output / "rates.json").write_text(json.dumps(rates), encoding="utf-8")
        except Exception as error:
            failures.append(f"{type(error).__name__}: {error}")
            if isinstance(error, subprocess.CalledProcessError):
                failures.append((error.stderr or b"").decode("utf-8", "replace")[-2000:])
        finally:
            if server:
                server.cancel.set()
                server.shutdown()
                server.server_close()
                http_thread.join(2)
            gaps = [b - a for a, b in zip(ticks, ticks[1:])]
            (args.output / "result.json").write_text(json.dumps({
                "cases": results, "failures": failures, "qt_ticks": len(ticks),
                "qt_gap_ms_max": max(gaps, default=0) * 1000,
            }, indent=2), encoding="utf-8")
            app.quit()
    worker = threading.Thread(target=work, name="movie-m1-lab")
    worker.start()
    app.exec()
    worker.join(5)
    return int(bool(failures) or worker.is_alive())


if __name__ == "__main__":
    raise SystemExit(main())
