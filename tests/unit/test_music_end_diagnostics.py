"""End diagnostics retain IPC behavior and never log private media fields."""
import json
from unittest.mock import Mock, patch

import pytest

from app.music.mpv import MpvController
from tests.unit.test_music_next_play import controller
from app.music.queue import PlayMode


@pytest.mark.parametrize("reason", ["eof", "stop", "error", "quit"])
def test_end_file_reason_is_logged_without_consuming_the_command_response(reason):
    mpv = MpvController("unused-test-mpv")
    mpv._running = True
    mpv._pipe = Mock()
    mpv._pipe_available = lambda: 1
    mpv._pipe.readline.side_effect = [json.dumps(dict(event="end-file", reason=reason, filename="private-name",
                                                     file_error="private-token", playlist_entry_id=1)).encode(),
                                    b'{"request_id":1,"error":"success","data":true}\n']
    log = Mock()
    try:
        with patch("app.core.logger.get_logger", return_value=log):
            result = mpv._send_command("get_property", ["idle-active"])
        assert result == {"request_id": 1, "error": "success", "data": True}
        assert log.info.call_count == 1
        text = log.info.call_args.args[0]
        assert f"reason={reason}" in text and "entry_id=1" in text
        assert "private-name" not in text and "private-token" not in text
    finally:
        mpv._pipe = None
        mpv._running = False


def test_auto_next_logs_queue_mode_and_cursor_without_song_names():
    ctrl = controller(PlayMode.SEQUENTIAL)
    log = Mock()
    with patch("app.music.controller.logger", log):
        ctrl._auto_next()
    messages = [call.args[0] for call in log.info.call_args_list]
    assert any("queue_size=3" in text and "index=0" in text for text in messages)
    assert any("queue_size=3" in text and "index=1" in text for text in messages)
    assert any("mode=sequential" in text for text in messages)
    assert any("has_current_song=" in text for text in messages)
    assert all(" playing=" not in text for text in messages)
    assert ctrl.queue.get_current()["title"] == "B"


def test_a_failed_diagnostic_write_cannot_break_the_command_response():
    mpv = MpvController("unused-test-mpv")
    mpv._running = True
    mpv._pipe = Mock()
    mpv._pipe_available = lambda: 1
    mpv._pipe.readline.side_effect = [b'{"event":"end-file","reason":"eof"}',
                                    b'{"request_id":1,"error":"success","data":true}']
    log = Mock()
    log.info.side_effect = PermissionError("isolated log failure")
    try:
        with patch("app.core.logger.get_logger", return_value=log):
            assert mpv._send_command("get_property", ["idle-active"])["error"] == "success"
    finally:
        mpv._pipe = None
        mpv._running = False
