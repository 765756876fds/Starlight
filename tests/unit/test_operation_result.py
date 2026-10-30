from unittest.mock import MagicMock
import pytest
from app.core.operation_result import media_result, OperationStatus


@pytest.mark.parametrize("value,status", [(True, OperationStatus.CONFIRMED),
    (False, OperationStatus.FAILED), (None, OperationStatus.UNKNOWN),
    ({"ok": True}, OperationStatus.UNKNOWN), (MagicMock(), OperationStatus.UNKNOWN)])
def test_no_truthiness_assumptions(value, status):
    assert media_result(value, label="音乐", action="停止").status is status


def test_load_acceptance_does_not_claim_playback():
    result = media_result(True, label="电影", action="播放", accepted_only=True)
    assert result.status is OperationStatus.ACCEPTED
    assert "已播放" not in result.message


@pytest.mark.parametrize("paused,expected", [(True, True), (False, None), (None, None)])
def test_movie_pause_feedback_requires_actual_state(paused, expected):
    from app.movie.movie_controller import MovieController
    c = MovieController.__new__(MovieController)
    c._pausing = False
    c._current = {"title": "test"}
    c.mpv = MagicMock()
    c.mpv.get_property.return_value = paused
    c._write_state = MagicMock()
    c._notify_voice = MagicMock()
    assert c.pause() is expected
    c.mpv.pause.assert_called_once()
    assert c._notify_voice.call_count == (1 if expected is True else 0)
    assert c._pausing is False


@pytest.mark.parametrize("value,word", [(True, "已停止"), (False, "未成功"), (None, "无法确认")])
def test_voice_music_stop_executes_and_reports_once(value, word):
    from app.voice.runtime import VoiceRuntime
    from app.voice.intent import Intent
    from app.core.command_bus import CommandType
    rt = VoiceRuntime.__new__(VoiceRuntime)
    rt.command_bus = MagicMock()
    rt.event_bus = MagicMock()
    rt.command_bus.execute.return_value = value
    rt._enqueue_speak = MagicMock()
    rt._execute_intent(Intent(CommandType.MUSIC_STOP), "停止音乐")
    rt.command_bus.execute.assert_called_once()
    rt._enqueue_speak.assert_called_once()
    assert word in rt._enqueue_speak.call_args.args[0]


@pytest.mark.parametrize("paused,expected", [(False, True), (True, None), (None, None)])
def test_movie_resume_requires_actual_state(paused, expected):
    from app.movie.movie_controller import MovieController
    c = MovieController.__new__(MovieController)
    c._current = {"title": "test"}
    c.mpv = MagicMock()
    c.mpv.get_property.return_value = paused
    c._write_state = MagicMock()
    c._notify_voice = MagicMock()
    assert c.resume() is expected
    c.mpv.play.assert_called_once()
    assert c._write_state.call_count == (1 if expected is True else 0)
