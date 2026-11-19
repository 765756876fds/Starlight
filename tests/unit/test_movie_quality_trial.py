from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from app.movie.movie_controller import MovieController


def controller():
    c = MovieController.__new__(MovieController)
    c._config = {"movie.source_quality_enabled": False}
    c._playback_gen, c._current_index = 1, 0
    c.mpv = Mock(ipc_pipe="test-only-not-opened", _process=Mock(poll=Mock(return_value=None)))
    return c


def test_runtime_switch_is_default_off_idempotent_and_never_operates_player():
    c = controller()
    before = dict(c._config)
    c._start_playback_diagnostics("external", "https://user:secret@example.invalid/a?token=secret",
                                  media_url="https://example.invalid/a?token=secret")
    assert not c.source_quality_trial_enabled()
    assert "secret" not in repr(c._source_quality_context)
    with patch("app.movie.playback_diagnostics.MoviePlaybackMonitor") as factory:
        assert c.set_source_quality_trial(True)
        assert c.source_quality_trial_enabled()
        factory.return_value.start.assert_called_once()
        assert c.set_source_quality_trial(True)
        assert factory.return_value.start.call_count == 1
        assert c.set_source_quality_trial(False)
        assert not c.source_quality_trial_enabled()
        factory.return_value.interrupt.assert_called_once_with(suspend=True)
    assert c._config == before
    # Owned-process liveness checks are allowed; player/IPC calls are not.
    assert c.mpv.mock_calls and all(c == call._process.poll() for c in c.mpv.mock_calls)


def test_late_enable_does_not_fabricate_startup_latency():
    c = controller()
    c._start_playback_diagnostics("cms", "fixture", media_url="https://example.invalid/a")
    with patch("app.movie.playback_diagnostics.MoviePlaybackMonitor"), patch(
        "app.movie.source_quality.SourceQualitySession"
    ) as tracker:
        c.set_source_quality_trial(True)
    assert tracker.call_args.kwargs["measure_startup"] is False


def test_changed_generation_or_process_cannot_reuse_old_observation_context():
    for change in ("generation", "process", "index"):
        c = controller()
        c._start_playback_diagnostics("cms", media_url="https://example.invalid/a")
        if change == "generation":
            c._playback_gen += 1
        elif change == "index":
            c._current_index += 1
        else:
            c.mpv._process = Mock()
        with patch("app.movie.playback_diagnostics.MoviePlaybackMonitor") as factory:
            assert c.set_source_quality_trial(True)
        factory.assert_not_called()


def test_switch_with_no_media_does_not_start_mpv_or_threads():
    c = controller()
    with patch("app.movie.playback_diagnostics.MoviePlaybackMonitor") as factory:
        assert c.set_source_quality_trial(True)
        assert c.set_source_quality_trial(False)
    factory.assert_not_called()
    assert c.mpv.mock_calls == []


def test_closed_controller_cannot_be_enabled():
    c = controller()
    c._media_voice_closed = True
    assert c.set_source_quality_trial(True) is False
    assert not c.source_quality_trial_enabled()


def test_pending_callback_after_off_is_not_current():
    c = controller()
    c._source_quality_history = SimpleNamespace(snapshot=lambda now: ())
    c._source_quality_latest = (1, SimpleNamespace(observed_at=0))
    c._playback_monitor = Mock(quality_current=Mock(return_value=False))
    assert c.set_source_quality_trial(False)
    assert c.source_quality_snapshot()["current"] is None
