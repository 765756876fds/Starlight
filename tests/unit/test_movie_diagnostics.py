import json
import threading
import time
from unittest.mock import MagicMock, Mock, patch

import pytest

from app.movie.movie_controller import _mpv_buffer_args
from app.movie.playback_diagnostics import (
    MoviePlaybackMonitor, PlaybackSummary, read_sample, sanitize,
)
from app.movie.source_manager import _check_hls


def config(value):
    return Mock(get=Mock(return_value=value))


def test_numeric_hls_cap_is_opt_in():
    original = _mpv_buffer_args(config({}))
    capped = _mpv_buffer_args(config({"hls_bitrate": 700000}))
    assert not any("hls-bitrate" in arg for arg in original)
    assert capped == original + ["--hls-bitrate=700000"]
    assert "--demuxer-max-bytes=256MiB" in capped
    assert "--cache-pause-wait=2" in capped


@pytest.mark.parametrize("value", [True, -1, 0, "auto", "700000", 1.5, None, 10**20])
def test_invalid_hls_cap_keeps_original(value):
    assert _mpv_buffer_args(config({"hls_bitrate": value})) == _mpv_buffer_args(config({}))


@pytest.mark.parametrize("body", [
    b"<html>try https://example.org/index.m3u8</html>",
    b"error: #EXTM3U no access",
])
def test_manifest_probe_rejects_html_or_embedded_marker(body):
    response = Mock(status_code=200)
    response.iter_content.return_value = iter([body])
    with patch("app.movie.source_manager.requests.get", return_value=response):
        assert not _check_hls("https://example.invalid/test", timeout=.2)
    response.close.assert_called_once()


@pytest.mark.parametrize("status,body,valid", [
    (200, b"#EXTM3U\n#EXTINF:2,\nmissing.ts\n", True),
    (503, b"#EXTM3U\n", False),
    (200, b"\xef\xbb\xbf#EXTM3U\n", True),
])
def test_probe_does_not_claim_segments_work(status, body, valid):
    response = Mock(status_code=status)
    response.iter_content.return_value = iter([body])
    with patch("app.movie.source_manager.requests.get", return_value=response) as request:
        assert _check_hls("https://example.invalid/test", timeout=.2) is valid
    assert request.call_count == 1  # No speculative segment fetching.
    response.close.assert_called_once()


def test_manifest_read_error_closes_response():
    response = Mock(status_code=200)
    response.iter_content.side_effect = OSError("broken stream")
    with patch("app.movie.source_manager.requests.get", return_value=response):
        assert not _check_hls("https://example.invalid/test", timeout=.2)
    response.close.assert_called_once()


def test_missing_pipe_is_bounded():
    start = time.monotonic()
    assert read_sample(r"\\.\pipe\missing_movie_m1_test_pipe") == {}
    assert time.monotonic() - start < 1


def fake_pipe(payload):
    pipe = MagicMock()
    pipe.__enter__.return_value = pipe
    remaining = [payload]
    def available(_handle, _buf, _size, _read, count, _left):
        count._obj.value = len(remaining[0])
        return True
    def read(size):
        chunk, remaining[0] = remaining[0][:size], remaining[0][size:]
        return chunk
    pipe.read.side_effect = read
    return pipe, Mock(PeekNamedPipe=Mock(side_effect=available))


def test_partial_json_respects_deadline_and_closes_own_pipe():
    pipe, kernel = fake_pipe(b'{"request_id":1')
    started = time.monotonic()
    with patch("builtins.open", return_value=pipe), patch(
        "ctypes.WinDLL", return_value=kernel
    ), patch("msvcrt.get_osfhandle", return_value=123):
        assert read_sample("isolated", timeout=.04) == {}
    assert time.monotonic() - started < .3
    pipe.__exit__.assert_called_once()
    commands = [json.loads(line)["command"] for line in pipe.write.call_args.args[0].splitlines()]
    assert all(command[0] == "get_property" for command in commands)


def test_changed_path_within_snapshot_is_discarded():
    from app.movie.playback_diagnostics import PROPERTIES
    values = ["private-a", *[None for _ in PROPERTIES], "private-b"]
    payload = b"".join((json.dumps({
        "request_id": i, "error": "success", "data": value,
    }) + "\n").encode() for i, value in enumerate(values, 1))
    pipe, kernel = fake_pipe(payload)
    with patch("builtins.open", return_value=pipe), patch(
        "ctypes.WinDLL", return_value=kernel
    ), patch("msvcrt.get_osfhandle", return_value=123):
        assert read_sample("isolated", timeout=.04) == {}


def sample(**kw):
    return {"time-pos": 1., "pause": False, "seeking": False,
            "paused-for-cache": False, **kw}


def test_summary_separates_initial_buffer_pause_seek_and_rebuffer():
    summary = PlaybackSummary(0)
    assert summary.add(sample(**{"paused-for-cache": True}), 1)["status"] == "initial_cache_wait"
    assert summary.add(sample(**{"time-pos": 1.5}), 2)["status"] == "progressing"
    assert summary.add(sample(**{"pause": True}), 3)["status"] == "user_paused"
    assert summary.add(sample(**{"seeking": True}), 4)["status"] == "seeking"
    result = summary.add(sample(**{"paused-for-cache": True}), 5)
    assert result["status"] == "cache_wait"
    assert result["rebuffer_events"] == 1
    assert summary.add(sample(), 6)["cache_wait_s"] == 2  # initial + later wait


def test_decode_pressure_requires_cache_and_increasing_drops():
    summary = PlaybackSummary(0)
    summary.add(sample(**{"frame-drop-count": 10}), 1)
    result = summary.add(sample(**{
        "demuxer-cache-duration": 5, "frame-drop-count": 12,
    }), 2)
    assert result["status"] == "decode_or_render_pressure"
    assert summary.add(sample(**{"frame-drop-count": 14}), 3)["status"] == "unknown"


def test_unknown_is_not_zero_and_strings_are_not_logged():
    secret = "https://private.invalid/video?token=secret"
    values = sanitize({"hwdec-current": secret, "cache-speed": float("nan"),
                       "demuxer-cache-state": {"url": secret, "fw-bytes": 12}})
    assert values["cache-speed"] is None
    assert values["demuxer-cache-duration"] is None
    assert values["demuxer-cache-state"] == {"fw-bytes": 12}
    assert secret not in str(values)
    assert PlaybackSummary(0).add({}, 1) == {"status": "unavailable"}


def test_source_tag_discards_url_credentials_and_is_anonymous():
    from app.movie.playback_diagnostics import source_tag
    tag = source_tag("https://name:private@example.invalid/video?token=one")
    assert tag == source_tag("https://example.invalid/other?token=two")
    assert len(tag) == 12 and "private" not in tag
    assert source_tag("https://[invalid") == "unknown"


def test_controller_observer_failure_cannot_fail_playback():
    from app.movie.movie_controller import MovieController
    controller = MovieController.__new__(MovieController)
    controller._config = config(True)
    controller.mpv = Mock(ipc_pipe="isolated")
    controller._playback_gen = 1
    with patch("app.movie.playback_diagnostics.MoviePlaybackMonitor.start",
               side_effect=RuntimeError("observer only")), patch(
        "app.movie.movie_controller.logger"
    ) as logger:
        controller._start_playback_diagnostics("local")
    controller.mpv.loadfile.assert_not_called()
    logger.warning.assert_called_once_with(
        "[MoviePerf] observer_unavailable type=RuntimeError")


def test_monitor_nonblocking_replacement_stale_discard_and_shutdown():
    entered, release, emitted = threading.Event(), threading.Event(), threading.Event()
    calls, logs = [], []
    def reader(pipe):
        calls.append(pipe)
        if pipe == "old":
            entered.set()
            assert release.wait(1)
        return sample()
    def emit(value):
        logs.append(value)
        emitted.set()
    monitor = MoviePlaybackMonitor(emit, reader=reader, interval=.01, duration=.5)
    try:
        monitor.start("old", 1, lambda: True, "cms")
        assert entered.wait(1)
        original_thread = monitor._thread
        started = time.monotonic()
        monitor.start("new", 2, lambda: True, "external")
        assert time.monotonic() - started < .1
        release.set()
        assert emitted.wait(1)
        assert monitor._thread is original_thread
        assert all(entry["generation"] == 2 for entry in logs)
        assert calls[:2] == ["old", "new"]
    finally:
        release.set()
        monitor.close()
        monitor._thread.join(1)
    assert not monitor._thread.is_alive()


def test_closed_monitor_does_not_restart():
    monitor = MoviePlaybackMonitor(Mock())
    monitor.close()
    monitor.start("unused", 1, lambda: True, "local")
    assert monitor._thread is None


def test_observer_exception_isolated_and_worker_released():
    monitor = MoviePlaybackMonitor(Mock(), reader=Mock(side_effect=RuntimeError("test")))
    monitor.start("unused", 1, lambda: True, "local")
    monitor.close()
    monitor._thread.join(1)
    assert not monitor._thread.is_alive()
