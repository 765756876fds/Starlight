from dataclasses import asdict, replace
import json
import threading
import time
from unittest.mock import Mock

import pytest

from app.movie.movie_controller import MovieController
from app.movie.playback_diagnostics import MoviePlaybackMonitor
from app.movie.source_quality import (
    SourceQualityHistory, SourceQualitySession,
    compare_evidence, media_origin,
)


def sample(pos, **values):
    return {"time-pos": pos, "speed": 1, "pause": False, "seeking": False,
            "paused-for-cache": False, "core-idle": False, "eof-reached": False,
            "cache-speed": 100000, "demuxer-cache-duration": 10,
            "frame-drop-count": 0, "decoder-frame-drop-count": 0, **values}


def warm(tracker, seconds=32):
    for now in range(0, seconds + 1, 2):
        report = tracker.observe(sample(now), now)
    return report


def tracker():
    return SourceQualitySession("source", "origin", 1, 0)


def test_stable_requires_real_advancement_not_a_successful_manifest():
    t = tracker()
    assert t.observe({}, 0).state == "UNKNOWN"
    t = tracker()
    assert warm(t, 6).state == "OBSERVING"
    assert warm(tracker()).state == "STABLE"


def test_sustained_wait_lower_bound_and_one_cooldown_signal():
    t = tracker()
    warm(t, 10)
    first = t.observe(sample(10, **{"paused-for-cache": True, "core-idle": True}), 12)
    second = t.observe(sample(10, **{"paused-for-cache": True, "core-idle": True}), 14)
    third = t.observe(sample(10, **{"paused-for-cache": True, "core-idle": True}), 16)
    assert first.waits == 0
    assert second.waits == 1 and second.wait_lower_s == 2
    assert not second.advisory_eligible
    assert third.advisory_eligible and third.wait_lower_s == 4
    assert not t.observe(sample(10, **{"paused-for-cache": True}), 18).advisory_eligible
    t.dismiss(19)
    assert not t.observe(sample(10, **{"paused-for-cache": True}), 20).advisory_eligible


def test_two_confirmed_episodes_in_sixty_seconds():
    t = tracker()
    warm(t, 10)
    for now in (12, 14):
        a = t.observe(sample(10, **{"paused-for-cache": True}), now)
    assert a.waits == 1 and not a.advisory_eligible
    t.observe(sample(12), 16)
    t.observe(sample(12, **{"paused-for-cache": True}), 18)
    b = t.observe(sample(12, **{"paused-for-cache": True}), 20)
    assert b.waits == 2 and b.advisory_eligible


@pytest.mark.parametrize("changes", [
    {"seeking": True}, {"pause": True}, {"eof-reached": True},
    {"time-pos": 1000}, {"time-pos": -2}, {"speed": None},
])
def test_transitions_and_unknown_do_not_become_bad_source(changes):
    t = tracker()
    warm(t)
    m = sample(34)
    m.update(changes)
    report = t.observe(m, 34)
    assert report.state in ("EXCLUDED", "UNKNOWN")
    for now in (36, 38, 40):
        report = t.observe(sample(34, **{"paused-for-cache": True}), now)
    assert report.waits == 0 and not report.advisory_eligible


def test_startup_wait_never_generates_an_advisory():
    t = tracker()
    for now in range(0, 70, 2):
        r = t.observe(sample(0, **{"paused-for-cache": True, "core-idle": True}), now)
    assert r.waits == 0 and not r.advisory_eligible and r.startup_s is None


def test_initial_unknown_preserves_observed_load_latency():
    t = tracker()
    assert t.observe({}, 0).state == "UNKNOWN"
    t.observe(sample(0), 2)
    assert t.observe(sample(2), 4).startup_s == 4


def test_expired_episodes_and_cooldown_do_not_become_permanent_bad_state():
    t = tracker()
    warm(t, 10)
    for now in (12, 14):
        t.observe(sample(10, **{"paused-for-cache": True}), now)
    for now in range(16, 80, 2):
        report = t.observe(sample(now - 4), now)
    assert report.state == "STABLE" and not report.advisory_eligible


def test_optional_quality_callback_exception_does_not_stop_old_diagnostics():
    emitted = threading.Event()
    monitor = MoviePlaybackMonitor(lambda row: emitted.set(), reader=lambda _: sample(1),
                                   interval=.01, duration=.1)
    failing = Mock(side_effect=RuntimeError("quality hook"))
    try:
        monitor.start("fake", 1, lambda: True, "cms", on_sample=failing)
        assert emitted.wait(1)
        deadline = time.monotonic() + 1
        while monitor._quality_failed is None and time.monotonic() < deadline:
            time.sleep(.005)
        assert monitor._quality_failed is not None
        assert monitor._job is not None and monitor._thread.is_alive()
        assert not monitor.quality_current(1)
        assert failing.call_count == 1
    finally:
        monitor.close()
        monitor._thread.join(1)


def test_explicit_epoch_catches_seek_that_happens_between_samples():
    t = tracker()
    warm(t)
    report = t.observe(sample(34), 34, epoch=1)
    assert report.state == "EXCLUDED"
    assert t.observe(sample(34, **{"paused-for-cache": True}), 36, epoch=1).waits == 0


def test_missing_sample_and_long_gap_never_count_as_buffering():
    for missing, now in (({}, 34), (sample(40), 40)):
        t = tracker()
        warm(t)
        assert t.observe(missing, now).state == "UNKNOWN"
        assert t.observe(sample(40, **{"paused-for-cache": True}), now + 2).waits == 0


def test_decode_pressure_with_full_cache_is_not_source_failure():
    t = tracker()
    warm(t)
    r = t.observe(sample(34, **{"decoder-frame-drop-count": 10}), 34)
    assert r.state == "UNKNOWN" and r.reason == "decode_or_render_pressure"
    assert r.waits == 0


def test_resume_and_speed_changes_remain_conservative():
    t = tracker()
    for now in range(0, 34, 2):
        r = t.observe(sample(now * 2, speed=2), now)
    assert r.state == "STABLE"
    assert t.observe(sample(64, pause=True), 34).state == "EXCLUDED"
    assert not t.observe(sample(64, **{"paused-for-cache": True}), 36).advisory_eligible


def test_anonymous_origin_deduplicates_cms_names_and_strips_secrets():
    a = media_origin("https://user:secret@example.invalid/a.m3u8?token=one")
    assert a == media_origin("https://example.invalid:443/b.m3u8?token=two")
    assert a != media_origin("https://another.invalid/a.m3u8")
    assert media_origin("C:/private/video.mp4") is None
    assert media_origin("https://[invalid") is None
    assert "secret" not in a and "example" not in a


def test_comparison_prefers_verified_stability_over_peak_speed_and_preserves_unknown():
    good = warm(tracker())
    bad = replace(good, source="fast", origin="fast-origin", state="BUFFERING",
                  input_bps_median=10**9, waits=3)
    assert compare_evidence(bad, good, 34)[0] == "ALTERNATIVE_EVIDENCE_BETTER"
    assert compare_evidence(bad, None, 34)[0] == "KEEP_CURRENT"
    assert compare_evidence(None, good, 34)[0] == "KEEP_CURRENT"
    assert compare_evidence(bad, good, 34, pinned=True)[1] == "user_pinned"
    assert compare_evidence(bad, replace(good, origin=bad.origin), 34)[1] == "shared_media_origin"
    assert compare_evidence(bad, good, 1000)[1] == "stale_evidence"


def test_history_is_bounded_fresh_nonblocking_and_unknown_is_not_bad():
    h = SourceQualityHistory(limit=2, ttl=10)
    good = warm(tracker())
    assert h.update(replace(good, state="UNKNOWN")) is False
    assert h.snapshot(32) == ()
    for n in range(8):
        assert h.update(replace(good, source=str(n)))
    assert len(h.snapshot(32)) == 2 and h.snapshot(43) == ()
    with h._lock:
        started = time.monotonic()
        assert h.snapshot(32) is None
        assert not h.update(good)
        assert time.monotonic() - started < .1
    assert "time-pos" not in json.dumps(asdict(good))


def test_monitor_transition_drops_inflight_observation_and_closes():
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    rows = []
    def reader(pipe):
        if not entered.is_set():
            entered.set()
            assert release.wait(1)
        return sample(1)
    def collect(value, now, epoch):
        rows.append(epoch)
        observed.set()
    monitor = MoviePlaybackMonitor(Mock(), reader=reader, interval=.01)
    try:
        monitor.start("fake", 1, lambda: True, "cms", on_sample=collect)
        assert entered.wait(1)
        monitor.interrupt()
        release.set()
        assert observed.wait(1)
        assert all(epoch == 2 for epoch in rows)
        monitor.interrupt(suspend=True)
        assert not monitor.quality_current(2)
    finally:
        release.set()
        monitor.close()
        monitor._thread.join(1)
    assert not monitor._thread.is_alive()


def test_old_callback_cannot_be_read_as_current_after_manual_seek():
    controller = MovieController.__new__(MovieController)
    monitor = MoviePlaybackMonitor(Mock())
    controller._playback_monitor = monitor
    controller._source_quality_history = SourceQualityHistory()
    entered, release = threading.Event(), threading.Event()
    def reader(_):
        entered.set()
        assert release.wait(1)
        return sample(1)
    monitor._reader = reader
    monitor.start("fake", 1, lambda: True, "cms")
    try:
        assert entered.wait(1)  # Reader runs outside the monitor lock.
        report = replace(warm(tracker()), observed_at=time.monotonic())
        controller._source_quality_latest = 1, report
        assert controller.source_quality_snapshot()["current"] is report
        controller._quality_transition()
        # A late callback carrying the old revision still cannot revive the plan.
        controller._source_quality_latest = 1, report
        assert controller.source_quality_snapshot()["current"] is None
        controller.mpv = Mock()
        controller.get_duration = lambda: 60
        controller.seek(10)
        controller.mpv.seek.assert_called_once_with(10, "relative")
    finally:
        release.set()
        monitor.close()
        monitor._thread.join(1)


def test_history_limits_sessions_per_source():
    history = SourceQualityHistory()
    report = warm(tracker())
    for session in range(12):
        history.update(replace(report, session=session))
    assert [r.session for r in history.snapshot(32)] == [8, 9, 10, 11]


def test_quality_default_is_off_and_has_no_collector_or_new_media_call():
    controller = MovieController.__new__(MovieController)
    controller._config = Mock(get=Mock(return_value=False))
    controller.mpv = Mock()
    controller._start_playback_diagnostics("cms", "source", media_url="https://example.invalid/a")
    assert controller.source_quality_snapshot() is None
    controller.mpv.assert_not_called()
    assert not hasattr(controller, "_playback_monitor")


def test_quality_only_does_not_probe_local_files_or_cached_manifests():
    controller = MovieController.__new__(MovieController)
    controller._config = Mock(get=lambda key, default=None: key == "movie.source_quality_enabled")
    controller.mpv = Mock()
    controller._start_playback_diagnostics("local", media_url="C:/synthetic/video.mp4")
    assert controller.source_quality_snapshot() is None
    assert not hasattr(controller, "_playback_monitor")
