"""Owned MPV + generated localhost HLS; never a user film or source."""
import functools
import json
import threading
import time

import pytest
from PySide6.QtCore import QSettings, QTimer

from PySide6.QtWidgets import QListWidget

from app.ui.panels.movie_panel import MoviePanel
from tests.integration.test_movie_resume_mpv import (
    media as media, qapp as qapp, sessions as sessions,
)
from tools.movie_playback_lab import Handler, Server, generate

pytestmark = pytest.mark.integration


def test_runtime_notice_and_candidates_preserve_owned_playback(
    sessions, media, qapp, tmp_path, monkeypatch,
):
    monkeypatch.setattr("app.ui.settings_store.UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr("app.ui.widgets.video_player_window.QSettings",
                        lambda *a: QSettings(str(tmp_path / "video.ini"), QSettings.IniFormat))
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(qapp)
    rates = generate(media[0].parents[1] / "ffmpeg/ffmpeg.exe", tmp_path / "hls")
    server = Server(("127.0.0.1", 0), functools.partial(Handler, directory=str(tmp_path / "hls")))
    server.bytes_per_second = 1000000
    server.cancel = threading.Event()
    server.break_segments = False
    server.transferred = server.failures = 0
    http = threading.Thread(target=server.serve_forever, name="owned-quality-http", daemon=True)
    http.start()
    s = sessions("runtime-notice")
    s.mpv.extra_video_args += [
        "--cache=yes", "--demuxer-max-bytes=256KiB", "--demuxer-readahead-secs=2",
        "--cache-secs=2", "--cache-pause-initial=yes", "--cache-pause-wait=0.5",
    ]
    from app.ui.widgets.movie_home import MovieHome
    monkeypatch.setattr(MovieHome, "refresh_local", lambda *_: None)
    monkeypatch.setattr(MovieHome, "refresh_boards", lambda *_: None)
    monkeypatch.setattr(s.ctl, "embed_enabled", lambda: True)
    monkeypatch.setattr(s.ctl, "progressive_search_service", lambda: None)
    panel = MoviePanel(movie=s.ctl)
    panel._timer.stop()
    w = panel._video_win
    s.windows.append(w)
    w._timer.stop()
    w._hide_timer.stop()
    w._apply_mpv_mouse_passthrough = lambda: None
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: (ticks.append(time.monotonic()), w._poll_quality_notice()))

    def wait_until(predicate, budget):
        end = time.monotonic() + budget
        while time.monotonic() < end:
            qapp.processEvents()
            result = predicate()
            if result:
                return result
            time.sleep(.01)
        pytest.fail("bounded owned playback/presentation deadline")

    def report():
        view = s.ctl.source_quality_snapshot()
        return view["current"] if view else None

    try:
        assert not s.ctl.source_quality_trial_enabled()
        url = f"http://127.0.0.1:{server.server_port}/high/index.m3u8"
        original = {"vod_name": "Generated HLS", "first_url": url, "vod_id": "1",
                    "_source": "甲来源", "_source_id": "first"}
        alternate = dict(original, first_url=url.replace("/high/", "/low/"),
                         _source="乙来源", _source_id="second")
        panel._on_search_done([original, alternate])
        panel.stack_results.setCurrentWidget(panel.list_results)
        panel._on_item_activated(panel.list_results.item(0))
        wait_until(lambda: (s.mpv.get_property("time-pos", timeout=.2) or 0) > .3, 6)
        assert getattr(s.ctl, "_playback_monitor", None) is None
        process = s.mpv._process
        entry = s.mpv.get_property("playlist-entry-id", timeout=.3)
        w._playing = True
        w.show()
        qapp.processEvents()
        assert s.ctl.set_source_quality_trial(True)
        w._sync_quality_slot()
        timer.start(25)
        wait_until(lambda: report() and report().progress_s >= 8, 18)
        assert report().startup_s is None  # Enabled after actual load, never invented.
        assert not w.quality_notice.isVisible()
        with server.rate_lock:
            server.bytes_per_second = 20000
            server.next_send = time.monotonic()
        wait_until(lambda: report() and report().advisory_eligible, 18)
        stalled = report()
        wait_until(lambda: w.quality_notice.isVisible(), 1)
        assert stalled.state == "BUFFERING" and stalled.wait_lower_s >= 3
        w.quality_notice.view_button.click()
        candidates = panel._candidate_view[0]
        assert candidates.isVisible() and not candidates.isModal()
        assert "乙来源" in candidates.findChild(QListWidget).item(0).text()
        assert panel.list_results.count() == 2
        assert s.mpv.get_property("path", timeout=.3) == url
        candidates.reject()
        assert s.mpv._process is process
        assert s.mpv.get_property("playlist-entry-id", timeout=.3) == entry
        assert s.mpv.get_property("pause", timeout=.3) is False
        with server.rate_lock:
            server.bytes_per_second = 1000000
            server.next_send = time.monotonic()
        pos = s.mpv.get_property("time-pos", timeout=.3)
        wait_until(lambda: (s.mpv.get_property("time-pos", timeout=.2) or 0) > pos + .5, 6)
        assert s.ctl.set_source_quality_trial(False)
        w._poll_quality_notice()
        assert not w.quality_notice.isVisible() and not w._quality_slot.isVisible()
        assert s.ctl.source_quality_snapshot()["current"] is None
        assert s.mpv.get_property("playlist-entry-id", timeout=.3) == entry
        timer.stop()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert ticks and max(gaps) < .5
        (tmp_path / "runtime-result.json").write_text(json.dumps({
            "generated_bps": rates, "verified_progress_s": stalled.progress_s,
            "wait_lower_s": stalled.wait_lower_s, "waits": stalled.waits,
            "notice_shown": True, "candidate_dialog": True,
            "same_process_and_playlist_entry": True, "late_startup_unknown": True,
            "qt_gap_ms_max": max(gaps) * 1000, "local_http_bytes": server.transferred,
        }), encoding="utf-8")
    finally:
        timer.stop()
        w._playing = False
        w._on_stopped()
        w.close()
        panel._on_app_quit()
        panel.close()
        s.ctl.close()
        monitor = getattr(s.ctl, "_playback_monitor", None)
        if monitor is not None:
            monitor._thread.join(1)
            assert not monitor._thread.is_alive()
        server.cancel.set()
        server.shutdown()
        server.server_close()
        http.join(2)
        assert not http.is_alive() and not s.mpv.is_running()
