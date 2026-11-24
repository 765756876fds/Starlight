"""Real controller paths, isolated database and MPV responses; no media playback."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.unit.test_online_queue import _make_ctrl, _mk_track, _online_song
from app.core.logger import Logger


@pytest.fixture
def playback(tmp_path, monkeypatch):
    log = Logger(str(tmp_path / "logs"))
    monkeypatch.setattr("app.music.controller.logger", log)
    ctrl = _make_ctrl(tmp_path)
    monkeypatch.setattr(ctrl, "_confirm_playback", Mock())
    yield ctrl, log
    ctrl.stop()
    ctrl.db.close()
    log.close()


def messages(log):
    return [json.loads(line)["message"] for f in Path(log.log_dir).glob("*.jsonl")
            for line in f.read_text("utf8").splitlines()]


@pytest.mark.parametrize("media", ["local", "online"])
@pytest.mark.parametrize("outcome", ["confirmed", "failed", "unknown", "exception"])
def test_loadfile_diagnostic_reports_actual_response_without_private_media(playback, tmp_path, media, outcome):
    ctrl, log = playback
    responses = {"confirmed": {"error": "success"}, "failed": {"error": "command failed"}, "unknown": None}
    if outcome == "exception":
        ctrl.mpv.loadfile.side_effect = OSError("private-exception-detail")
    else:
        ctrl.mpv.loadfile.return_value = responses[outcome]
    if media == "local":
        path = tmp_path / "private-media-name.mp3"
        path.write_bytes(b"isolated controller test only")
        song = dict(title="private-title", path=str(path), filename=path.name)
        song["id"] = ctrl.db.insert_song(song)
        ctrl._play_song(song)
        ctrl.mpv.loadfile.assert_called_once_with(str(path))
    else:
        track = _mk_track(name="private-title")
        if outcome == "confirmed":
            assert ctrl._apply_stream(track, "https://example.invalid/private?token=private-token", None)
        else:
            with pytest.raises((OSError, RuntimeError)):
                ctrl._apply_stream(track, "https://example.invalid/private?token=private-token", None)
    stages = [m for m in messages(log) if m.startswith("music_playback ")]
    expected = "failed" if outcome == "exception" else outcome
    assert any(f"stage=loadfile result={expected} media={media}" in m for m in stages)
    assert all("request_id=" in m and "queue_size=" in m and "index=" in m and "mode=" in m for m in stages)
    assert all(word not in "\n".join(stages) for word in ["private-title", "private-media-name", "private-token", "private-exception-detail"])
    assert (ctrl._current_song is not None) == (outcome == "confirmed")


def test_online_resolution_without_url_records_failure_before_scheduling_skip(playback, monkeypatch):
    ctrl, log = playback
    ctrl.online_manager = Mock()
    ctrl.online_manager.get_play_url.return_value = None
    ctrl._resolver = Mock()
    ctrl._resolver.resolve.return_value = None
    skip = Mock()
    monkeypatch.setattr(ctrl, "_schedule_skip_online", skip)
    song = _online_song()
    ctrl._resolve_online_worker(song, "generated test", ctrl._play_request_id)
    skip.assert_called_once_with(song, ctrl._play_request_id)
    assert not ctrl.mpv.loadfile.called
    assert any("stage=resolve result=no_url" in m for m in messages(log))


def test_diagnostic_failure_does_not_change_a_successful_load(playback, monkeypatch):
    ctrl, _ = playback
    track = _mk_track()
    with monkeypatch.context() as patcher:
        patcher.setattr("app.music.controller.logger.info", Mock(side_effect=OSError("log unavailable")))
        result = ctrl._apply_stream(track, "https://example.invalid/generated", None)
    assert result is ctrl._current_song
    ctrl.mpv.loadfile.assert_called_once()


def test_resolved_url_with_rejected_load_is_not_reported_as_resolution_failure(playback, monkeypatch):
    ctrl, log = playback
    ctrl.online_manager = Mock()
    ctrl.online_manager.get_play_url.return_value = {"url": "https://example.invalid/generated"}
    ctrl._resolver = Mock()
    ctrl.mpv.loadfile.return_value = {"error": "command failed"}
    monkeypatch.setattr(ctrl, "_schedule_skip_online", Mock())
    ctrl._resolve_online_worker(_online_song(), "generated query", ctrl._play_request_id)
    stages = [m for m in messages(log) if m.startswith("music_playback ")]
    assert any("stage=resolve result=ready" in m for m in stages)
    assert any("stage=loadfile result=failed" in m for m in stages)
    assert any("stage=online_play result=failed" in m for m in stages)
    assert not any("stage=resolve result=failed" in m for m in stages)
    assert ctrl._current_song is None


def test_stale_resolution_logs_original_request_without_loading_or_skipping(playback, monkeypatch):
    ctrl, log = playback
    ctrl.online_manager = Mock()
    ctrl.online_manager.get_play_url.return_value = {"url": "https://example.invalid/generated"}
    ctrl._resolver = Mock()
    skip = Mock()
    monkeypatch.setattr(ctrl, "_schedule_skip_online", skip)
    ctrl._play_request_id = 2
    ctrl._resolve_online_worker(_online_song(), "generated query", 1)
    assert not ctrl.mpv.loadfile.called and not skip.called
    assert any("stage=resolve result=stale" in m and "request_id=1 " in m for m in messages(log))
