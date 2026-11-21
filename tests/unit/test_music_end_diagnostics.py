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


def test_diagnostic_observer_keeps_split_messages_and_does_not_touch_command_pipe():
    mpv = MpvController("unused-test-mpv")
    mpv._running = True
    mpv._pipe, mpv._event_pipe = Mock(), Mock()
    first, second = b'{"event":"end-', b'file","reason":"eof","playlist_entry_id":7}\n'
    mpv._event_pipe.read.side_effect = [first, second]
    log = Mock()
    try:
        with patch.object(MpvController, "_pipe_available", side_effect=[len(first), len(second)]), \
                patch("app.core.logger.get_logger", return_value=log):
            mpv.poll_end_events()
            assert not log.info.called
            mpv.poll_end_events()
            assert log.info.call_count == 1
            assert "reason=eof entry_id=7" in log.info.call_args.args[0]
            mpv._log_end_file(dict(reason="eof", playlist_entry_id=7))
            assert log.info.call_count == 1
        assert not mpv._pipe.write.called and not mpv._pipe.readline.called
        assert not mpv._event_buffer
    finally:
        event_pipe = mpv._event_pipe
        mpv.stop_process()
        event_pipe.close.assert_called_once()
        assert mpv._event_pipe is None


def test_diagnostic_observer_disconnect_closes_only_its_own_connection():
    mpv = MpvController("unused-test-mpv")
    mpv._running = True
    command, observer = Mock(), Mock()
    mpv._pipe, mpv._event_pipe = command, observer
    try:
        with patch.object(MpvController, "_pipe_available", return_value=-1):
            mpv.poll_end_events()
        observer.close.assert_called_once()
        assert mpv._event_pipe is None and not command.close.called
        assert mpv._running
    finally:
        mpv._pipe = None
        mpv._running = False


def test_missing_diagnostic_connection_cannot_break_normal_ipc():
    mpv = MpvController("unused-test-mpv")
    mpv._running = True
    mpv._pipe = Mock()
    mpv._pipe_available = lambda: 1
    mpv._pipe.readline.return_value = b'{"request_id":1,"error":"success","data":true}\n'
    try:
        with patch("builtins.open", side_effect=FileNotFoundError("observer unavailable")):
            mpv.poll_end_events()
        assert mpv._event_pipe is None
        assert mpv._send_command("get_property", ["idle-active"])["data"] is True
    finally:
        mpv._pipe = None
        mpv._running = False
