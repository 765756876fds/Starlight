"""Quoted commands and speed results through the real MPV wrapper, without IPC."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.command_bus import CommandBus, CommandType
from app.core.state_store import StateNamespace
from app.movie.movie_controller import MovieController
from app.music.controller import MusicController
from app.music.mpv import MpvController
from app.music.recording_mpv import RecordingMpv
from app.voice.intent import Intent, IntentRouter
from app.voice.runtime import VoiceRuntime


def voice_runtime():
    runtime = VoiceRuntime.__new__(VoiceRuntime)
    runtime.command_bus = CommandBus()
    runtime.event_bus = MagicMock()
    runtime._enqueue_speak = MagicMock()
    return runtime


def speed_controller(response):
    # Only the transport is fake: exercise MpvController.set_property itself.
    player = MpvController.__new__(MpvController)
    player._send_command = MagicMock(return_value=response)
    player._properties = {}
    controller = MusicController.__new__(MusicController)
    controller._mpv_available = True
    controller.mpv = player
    controller._speed = 1.0
    controller.state_store = MagicMock()
    return controller


def assert_automation_not_executed(text):
    runtime = voice_runtime()
    handler = MagicMock(return_value={"ok": True, "enabled": True})
    runtime.command_bus.register(CommandType.AUTO_QUEUE_SET, handler)
    intent = IntentRouter({}).parse(text)
    assert intent.intent_type in ("system.unknown", "system.clarify")
    runtime._execute_intent(intent, text)
    handler.assert_not_called()
    assert runtime.command_bus.get_history() == []


def test_single_quoted_game_action_must_not_execute():
    assert_automation_not_executed("'打开自动排队'")


@pytest.mark.parametrize("text", [
    '"打开自动排队"', "“打开自动排队”", "「打开自动排队」", "『打开自动排队』",
    "  '打开自动排队'  ", "'打开\n自动排队'", "不要打开自动排队", "如何打开自动排队", "打开自动排队？",
])
def test_existing_quote_question_and_negation_protection_is_preserved(text):
    assert_automation_not_executed(text)


def test_unquoted_game_command_executes_exactly_once():
    runtime = voice_runtime()
    handler = MagicMock(return_value={"ok": True, "enabled": True, "client_ready": False})
    runtime.command_bus.register(CommandType.AUTO_QUEUE_SET, handler)
    runtime._execute_intent(IntentRouter({}).parse("打开自动排队"), "打开自动排队")
    handler.assert_called_once()
    assert handler.call_args.args[0].params == {"enabled": True}
    runtime._enqueue_speak.assert_called_once()


@pytest.mark.parametrize("title", ["Don't Stop Believing", "O'Connor", "I'm Yours", "It's My Life"])
def test_english_apostrophe_in_media_title_is_not_a_quote_wrapper(title):
    intent = IntentRouter({}).parse("播放" + title)
    assert intent.intent_type == CommandType.MUSIC_SEARCH_PLAY
    assert intent.params == {"query": title}


@pytest.mark.parametrize("response", [{"error": "success"}, {"error": "property unavailable"}, None])
def test_set_property_preserves_transport_response(response):
    controller = speed_controller(response)
    assert controller.mpv.set_property("speed", 1.5) is response
    controller.mpv._send_command.assert_called_once_with("set_property", ["speed", 1.5])


def test_speed_explicit_success_updates_confirmed_state():
    controller = speed_controller({"error": "success"})
    assert controller.set_speed(1.5) is True
    assert controller.get_speed() == 1.5
    controller.state_store.set.assert_called_once_with(StateNamespace.MUSIC, "speed", 1.5)
    controller.mpv._send_command.assert_called_once_with("set_property", ["speed", 1.5])


def test_speed_ipc_error_must_not_confirm_success():
    controller = speed_controller({"error": "property unavailable"})
    assert controller.set_speed(1.5) is False
    assert controller.get_speed() == 1.0
    controller.state_store.set.assert_not_called()


def test_speed_ipc_no_response_must_remain_unknown():
    controller = speed_controller(None)
    assert controller.set_speed(1.5) is None
    assert controller.get_speed() == 1.0
    controller.state_store.set.assert_not_called()


@pytest.mark.parametrize("response", [True, {}, {"error": None}])
def test_untrusted_speed_response_does_not_change_state(response):
    controller = speed_controller(response)
    assert controller.set_speed(1.5) is None
    assert controller.get_speed() == 1.0
    controller.state_store.set.assert_not_called()


@pytest.mark.parametrize("error", [RuntimeError("IPC disconnected"), TimeoutError("IPC timeout")])
def test_speed_exception_preserves_state_and_is_not_swallowed(error):
    controller = speed_controller(None)
    controller.mpv._send_command.side_effect = error
    with pytest.raises(type(error)):
        controller.set_speed(1.5)
    assert controller.get_speed() == 1.0
    controller.state_store.set.assert_not_called()


@pytest.mark.parametrize("response,message", [
    ({"error": "success"}, None),
    ({"error": "property unavailable"}, "倍速设置未成功，请检查播放器状态"),
    (None, "暂时无法确认倍速设置结果"),
])
def test_voice_speed_feedback_uses_real_controller_result(response, message):
    controller = speed_controller(response)
    runtime = voice_runtime()
    runtime.command_bus.register(CommandType.MUSIC_SET_SPEED,
                                 lambda command: controller.set_speed(command.params["speed"]))
    runtime._execute_intent(Intent(CommandType.MUSIC_SET_SPEED, params={"speed": 1.5}), "倍速一点五倍")
    controller.mpv._send_command.assert_called_once()
    if message is None:
        runtime._enqueue_speak.assert_not_called()
        assert controller.get_speed() == 1.5
    else:
        runtime._enqueue_speak.assert_called_once_with(message, "voice")
        assert controller.get_speed() == 1.0


@pytest.mark.parametrize("response", [{"error": "success"}, {"error": "property unavailable"}, None])
@pytest.mark.parametrize("method,args,command,params", [
    ("play", (), "set_property", ["pause", False]),
    ("pause", (), "set_property", ["pause", True]),
    ("toggle_pause", (), "cycle", ["pause"]),
    ("set_volume", (30,), "set_property", ["volume", 30.0]),
    ("seek", (10,), "seek", [10, "relative"]),
])
def test_other_mpv_controls_keep_existing_calls_and_return_values(response, method, args, command, params):
    player = speed_controller(response).mpv
    assert getattr(player, method)(*args) is None
    player._send_command.assert_called_once_with(command, params)


@pytest.mark.parametrize("response", [{"error": "success"}, {"error": "property unavailable"}, None])
def test_recording_proxy_keeps_property_response(response):
    player = speed_controller(response).mpv
    recorder = MagicMock()
    proxy = RecordingMpv(player, recorder)
    assert proxy.set_property("speed", 1.5) is response
    player._send_command.assert_called_once()
    recorder.record.assert_called_once()


@pytest.mark.parametrize("response", [{"error": "success"}, {"error": "property unavailable"}, None])
def test_movie_speed_caller_keeps_existing_return_contract(response):
    player = speed_controller(response).mpv
    player.is_running = MagicMock(return_value=True)
    assert MovieController.set_speed(SimpleNamespace(mpv=player), 1.5) is None
    player._send_command.assert_called_once_with("set_property", ["speed", 1.5])
