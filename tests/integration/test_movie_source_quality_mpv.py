"""Generated HLS + localhost only; one owned MPV, no online provider/user media."""
import functools
import json
import threading
import time

import pytest
from PySide6.QtCore import QTimer

from tests.integration.test_movie_resume_mpv import (
    media as media, qapp as qapp, sessions as sessions,
)
from tools.movie_playback_lab import Handler, Server, generate
from app.movie.playback_diagnostics import read_sample

pytestmark = pytest.mark.integration


def test_owned_mpv_recovery_does_not_revive_old_quality_results(sessions, media, qapp):
    server = Server(("127.0.0.1", 0), functools.partial(Handler, directory=str(media[1].parent)))
    server.cancel = threading.Event()
    server.break_segments = False
    server.bytes_per_second = 1000000
    server.transferred = server.failures = 0
    http = threading.Thread(target=server.serve_forever, daemon=True)
    http.start()
    s = sessions("quality-recovery")
    original_get = s.ctl._config.get
    s.ctl._config.get = lambda key, default=None: (
        True if key == "movie.source_quality_enabled" else original_get(key, default))
    from urllib.parse import quote
    url = f"http://127.0.0.1:{server.server_port}/" + quote(media[1].name)
    try:
        assert s.ctl.play({"vod_name": "Generated recovery", "first_url": url})
        old = s.mpv._process
        old.kill()  # Exact process owned by this test; never global taskkill.
        old.wait(2)
        assert s.ctl.restart_current()
        deadline = time.monotonic() + 8
        while s.ctl._recover_pending and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.02)
        assert not s.ctl._recover_pending and s.mpv.is_running()
        assert s.mpv._process is not old
        assert s.ctl.source_quality_snapshot()["current"] is None
        deadline = time.monotonic() + 3
        result = {}
        while time.monotonic() < deadline:
            result = read_sample(s.mpv.ipc_pipe)
            if result.get("time-pos") is not None:
                break
            time.sleep(.02)
        assert result.get("time-pos") is not None
    finally:
        s.ctl.close()
        monitor = getattr(s.ctl, "_playback_monitor", None)
        if monitor is not None:
            monitor._thread.join(1)
            assert not monitor._thread.is_alive()
        server.cancel.set()
        server.shutdown()
        server.server_close()
        http.join(2)
        assert not s.mpv.is_running() and not http.is_alive()


def test_actual_sustained_buffer_is_distinct_from_seek_and_gui_stays_responsive(
    sessions, media, qapp, tmp_path,
):
    ffmpeg = media[0].parents[1] / "ffmpeg/ffmpeg.exe"
    rates = generate(ffmpeg, tmp_path / "generated-hls")
    server = Server(("127.0.0.1", 0), functools.partial(
        Handler, directory=str(tmp_path / "generated-hls")))
    server.bytes_per_second = 1000000
    server.cancel = threading.Event()
    server.break_segments = False
    server.transferred = server.failures = 0
    http = threading.Thread(target=server.serve_forever, daemon=True)
    http.start()
    s = sessions("owned-quality")
    original_get = s.ctl._config.get
    s.ctl._config.get = lambda key, default=None: (
        True if key == "movie.source_quality_enabled" else original_get(key, default))
    s.mpv.extra_video_args += [
        "--cache=yes", "--demuxer-max-bytes=256KiB", "--demuxer-readahead-secs=2",
        "--cache-secs=2", "--cache-pause-initial=yes", "--cache-pause-wait=0.5",
    ]
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(25)

    def wait_report(predicate, budget):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            qapp.processEvents()
            view = s.ctl.source_quality_snapshot()
            report = view["current"] if view else None
            if report is not None and predicate(report):
                return report
            time.sleep(.02)
        pytest.fail("bounded real quality evidence deadline")

    def control(fn, *args):
        # Match the existing movie UI: legacy seek/stop IPC runs off the GUI thread.
        results, errors = [], []
        def run():
            try:
                results.append(fn(*args))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run, name="owned-movie-control")
        worker.start()
        deadline = time.monotonic() + 4
        while worker.is_alive() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.01)
        worker.join(.01)
        assert not worker.is_alive() and not errors
        return results[0]

    try:
        assert s.ctl.play({
            "vod_name": "Generated isolated HLS", "_source": "synthetic CMS",
            "first_url": f"http://127.0.0.1:{server.server_port}/high/index.m3u8",
        })
        stable = wait_report(lambda r: r.state == "STABLE", 43)
        assert stable.startup_s is not None and stable.waits == 0
        assert s.ctl.source_quality_snapshot()["history"]
        with server.rate_lock:
            server.bytes_per_second = 20000
            server.next_send = time.monotonic()
        stalled = wait_report(lambda r: r.advisory_eligible, 18)
        assert stalled.state == "BUFFERING" and stalled.wait_lower_s >= 3
        assert stalled.waits >= 1
        # Restore fast local delivery, then invalidate quality on an explicit seek.
        with server.rate_lock:
            server.bytes_per_second = 1000000
            server.next_send = time.monotonic()
        control(s.ctl.seek, 20, "absolute")
        immediate = s.ctl.source_quality_snapshot()["current"]
        # The background observer may already return a NEW transition snapshot
        # while seek IPC completes; it must never expose the old buffering alert.
        assert immediate is None or (immediate.state == "EXCLUDED"
                                     and not immediate.advisory_eligible)
        excluded = wait_report(lambda r: r.state == "EXCLUDED", 3)
        assert not excluded.advisory_eligible
        monitor = s.ctl._playback_monitor
        assert control(s.ctl.stop) is True
        assert s.ctl.source_quality_snapshot()["current"] is None
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert ticks and max(gaps) < .5
        (tmp_path / "quality-result.json").write_text(json.dumps({
            "generated_bps": rates, "startup_s": stable.startup_s,
            "verified_stable_s": stable.progress_s,
            "wait_lower_s": stalled.wait_lower_s, "waits": stalled.waits,
            "advisory": stalled.advisory_eligible, "qt_gap_ms_max": max(gaps) * 1000,
            "local_http_bytes": server.transferred,
        }), encoding="utf-8")
    finally:
        timer.stop()
        s.ctl.close()
        monitor = getattr(s.ctl, "_playback_monitor", None)
        if monitor is not None:
            monitor._thread.join(1)
            assert not monitor._thread.is_alive()
        server.cancel.set()
        server.shutdown()
        server.server_close()
        http.join(2)
        assert not http.is_alive()
        assert not s.mpv.is_running()
