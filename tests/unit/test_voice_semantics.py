"""Semantic boundaries through the real parser/runtime/bus; no devices or HTTP."""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.command_bus import Command, CommandBus, CommandType as C
from app.core.operation_result import OperationStatus
from app.voice.intent import Intent, IntentRouter, RuleBasedIntent
from app.voice.registry import PARAMETERS, parameter_issue
from app.voice.runtime import VoiceRuntime
from app.voice.text import normalize_text


def runtime():
    rt = VoiceRuntime({"asr": {}, "intent": {}})
    rt.asr = MagicMock()
    rt.event_bus = MagicMock()
    rt.command_bus = CommandBus()
    rt._enqueue_speak = MagicMock()
    return rt


@pytest.mark.parametrize("text", ["把音乐声音调小一点", "音乐小声点", "音量降低一点", "请把音乐声音调小一点"])
def test_volume_synonyms_have_one_domain(text):
    assert RuleBasedIntent.match(text).intent_type == C.MUSIC_VOLUME_DOWN


@pytest.mark.parametrize("text", ["电影小声点", "把电影声音调小一点"])
def test_movie_volume_synonyms_stay_movie(text):
    assert RuleBasedIntent.match(text).intent_type == C.MOVIE_VOLUME_DOWN


@pytest.mark.parametrize("text,value", [("倍速1.5倍", 1.5), ("倍速１．２５倍", 1.25), ("倍速一点五倍", 1.5)])
def test_decimal_punctuation_is_not_removed(text, value):
    assert RuleBasedIntent.match(text).params["speed"] == value


def test_title_spaces_and_negation_are_preserved():
    assert RuleBasedIntent.match("播放 Don't Stop Me Now").params["query"] == "Don't Stop Me Now"
    assert RuleBasedIntent.match("播放不要说话").params["query"] == "不要说话"
    assert normalize_text("不要随机播放") == "不要随机播放"


@pytest.mark.parametrize("text", ["不要随机播放", "有人说随机播放", "我说音量60", "不要音量减10", "请不要打开自动排队", "别记录石头人闪现", "不要取消计时"])
def test_negation_and_narration_do_not_execute(text):
    assert IntentRouter({}).parse(text).intent_type == "system.unknown"


@pytest.mark.parametrize("text", ["播放音乐，然后关闭自动排队", "暂停电影，打开自动连招", "下一首并且上一首"])
def test_multiple_commands_request_clarification_without_execution(text):
    rt = runtime()
    intent = rt.intent_router.parse(text)
    assert intent.intent_type == "system.clarify"
    rt._execute_intent(intent, text)
    assert rt.command_bus.get_history() == []
    assert "一次" in rt._enqueue_speak.call_args.args[0]


@pytest.mark.parametrize("text,expected", [("静音", C.MUSIC_VOLUME_SET), ("电影静音", C.MOVIE_VOLUME_SET)])
def test_mute_sets_zero(text, expected):
    intent = RuleBasedIntent.match(text)
    assert (intent.intent_type, intent.params) == (expected, {"volume": 0})


@pytest.mark.parametrize("text", ["电影音量150", "音乐音量一百零一", "电影快进0秒", "电影后退80秒"])
def test_invalid_parameters_never_reach_controller_defaults(text):
    rt = runtime()
    intent = rt.intent_router.parse(text)
    assert parameter_issue(intent.intent_type, intent.params) is not None
    rt._execute_intent(intent, text)
    assert rt.command_bus.get_history() == []
    rt._enqueue_speak.assert_called_once()


@pytest.mark.parametrize("first,reply,command,value", [("倍速", "一点五倍", C.MUSIC_SET_SPEED, 1.5),
    ("电影音量", "五十", C.MOVIE_VOLUME_SET, 50),
    ("睡眠定时", "三十分钟", C.MUSIC_SLEEP_TIMER_SET, 1800)])
def test_parameter_reply_is_bounded_and_typed(first, reply, command, value):
    router = IntentRouter({})
    router.parse(first)
    result = router.parse(reply)
    assert result.intent_type == command
    assert result.params[PARAMETERS[command].key] == value
    assert router.parse(reply).intent_type == "system.unknown"


def test_parameter_context_expires_and_new_domain_clears_it(monkeypatch):
    monkeypatch.setattr("app.voice.intent.time.monotonic", lambda: 100)
    router = IntentRouter({})
    router.parse("电影音量")
    router.parse("打开自动排队")
    assert router.parse("五十").intent_type == "system.unknown"
    router.parse("倍速")
    monkeypatch.setattr("app.voice.intent.time.monotonic", lambda: 113)
    assert router.parse("一点五倍").intent_type == "system.unknown"


def test_explicit_music_pause_uses_only_music_controller():
    rt = runtime()
    calls = []
    rt.command_bus.register(C.MUSIC_PAUSE, lambda c: calls.append(c.command_type))
    rt.music_mpv_provider = lambda: SimpleNamespace(is_running=lambda: True,
        get_property=lambda *a, **k: True)
    rt.movie_mpv_provider = MagicMock()
    rt._execute_intent(rt.intent_router.parse("暂停音乐"), "暂停音乐")
    assert calls == [C.MUSIC_PAUSE]
    rt.movie_mpv_provider.assert_not_called()
    rt._enqueue_speak.assert_called_once_with("音乐已暂停", "voice_music_control")


@pytest.mark.parametrize("error,word", [(RuntimeError("private"), "未成功"), (TimeoutError(), "超时")])
def test_control_failure_feedback_once(error, word):
    rt = runtime()
    handler = MagicMock(side_effect=error)
    rt.command_bus.register(C.MUSIC_NEXT, handler)
    rt._execute_intent(Intent(C.MUSIC_NEXT), "下一首")
    handler.assert_called_once()
    rt._enqueue_speak.assert_called_once()
    assert word in rt._enqueue_speak.call_args.args[0]


def test_audio_replay_is_deduplicated_but_new_recording_is_not():
    rt = runtime()
    handler = MagicMock()
    rt.command_bus.register(C.MUSIC_NEXT, handler)
    rt.asr.transcribe.return_value = "下一首"
    rt._process_speech(b"capture-a", 16000)
    rt._process_speech(b"capture-a", 16000)
    rt._process_speech(b"capture-b", 16000)
    assert handler.call_count == 2


def test_late_asr_after_restart_cannot_execute():
    rt = runtime()
    entered, release = threading.Event(), threading.Event()
    def transcribe(*args):
        entered.set()
        assert release.wait(3)
        return "打开自动排队"
    rt.asr.transcribe.side_effect = transcribe
    worker = threading.Thread(target=rt._process_speech, args=(b"old", 16000))
    worker.start()
    try:
        assert entered.wait(3)
        rt._asr_generation += 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert rt.command_bus.get_history() == []


def test_late_search_result_does_not_override_new_voice_request():
    rt = runtime()
    callbacks = []
    rt.command_bus.execute_async = lambda c, on_done=None: callbacks.append(on_done)
    rt._execute_intent(Intent(C.MUSIC_SEARCH_PLAY, params={"query": "A"}), "播放A")
    rt._execute_intent(Intent(C.MUSIC_SEARCH_PLAY, params={"query": "B"}), "播放B")
    callbacks[0](None, RuntimeError("old"))
    rt._enqueue_speak.assert_not_called()
    callbacks[1]({"title": "B"}, None)
    rt._enqueue_speak.assert_called_once()
    assert "正在加载" in rt._enqueue_speak.call_args.args[0]


def test_async_bus_delivers_real_result_exactly_once():
    bus = CommandBus()
    callback = MagicMock()
    handler = MagicMock(return_value={"title": "A"})
    bus.register(C.MUSIC_SEARCH_PLAY, handler)
    worker = bus.execute_async(Command(C.MUSIC_SEARCH_PLAY), on_done=callback)
    worker.join(3)
    assert not worker.is_alive()
    handler.assert_called_once()
    callback.assert_called_once_with({"title": "A"}, None)


def test_movie_search_stale_after_query_never_starts_playback():
    from app.movie.movie_controller import MovieController
    valid = [True]
    c = MovieController.__new__(MovieController)
    c._notify_voice = MagicMock()
    c.play = MagicMock()
    def search(query):
        valid[0] = False
        return [{"vod_name": "old"}]
    c.search = search
    command = Command(C.MOVIE_SEARCH_PLAY, params={"query": "old"}, is_current=lambda: valid[0])
    assert c._cmd_search_play(command) is None
    c.play.assert_not_called()
    c._notify_voice.assert_not_called()


def test_automation_failure_dictionary_cannot_be_announced_as_success():
    rt = runtime()
    rt.command_bus.register(C.AUTO_QUEUE_SET, lambda c: {"ok": False, "enabled": True})
    rt._execute_intent(Intent(C.AUTO_QUEUE_SET, params={"enabled": True}), "打开自动排队")
    assert "未成功" in rt._enqueue_speak.call_args.args[0]


def test_cooldown_list_reuses_registered_business_handler():
    rt = runtime()
    rt.command_bus.register(C.COOLDOWN_LIST, lambda c: [{"champion_name": "石头人"}])
    rt._execute_intent(rt.intent_router.parse("查询当前技能计时"), "查询当前技能计时")
    rt._enqueue_speak.assert_called_once_with("当前有1个技能计时", "voice_cooldown")


@pytest.mark.parametrize("command,spec", PARAMETERS.items())
def test_shared_parameter_help_examples_match_real_rules(command, spec):
    intent = RuleBasedIntent.match(spec.example)
    assert intent.intent_type == command
    assert parameter_issue(command, intent.params) is None


def test_registry_uses_same_negation_boundary_as_parser():
    from app.voice.registry import match
    assert match("不要打开高潮模式") is None
    assert match("有人说下一首") is None
    assert match("打开高潮模式")["params"] == {"enabled": True}
    assert match("下一集")["executor"] == "MOVIE_NEXT_EPISODE"


def test_pending_speed_never_parses_number_inside_chat():
    router = IntentRouter({})
    router.parse("倍速")
    assert router.parse("我刚才说十五").intent_type == "system.unknown"


def test_unregistered_control_has_clear_unavailable_feedback():
    rt = runtime()
    rt._execute_intent(Intent(C.MUSIC_NEXT), "下一首")
    assert "尚未就绪" in rt._enqueue_speak.call_args.args[0]
    assert rt.command_bus.get_history() == []


def test_async_exception_reports_original_error_once():
    bus = CommandBus()
    error = RuntimeError("test")
    handler = MagicMock(side_effect=error)
    callback = MagicMock()
    bus.register(C.MUSIC_SEARCH_PLAY, handler)
    worker = bus.execute_async(Command(C.MUSIC_SEARCH_PLAY), on_done=callback)
    worker.join(3)
    assert not worker.is_alive()
    handler.assert_called_once()
    callback.assert_called_once_with(None, error)


def test_movie_start_rejection_has_failure_feedback_without_success_claim():
    from app.movie.movie_controller import MovieController
    c = MovieController.__new__(MovieController)
    c._notify_voice = MagicMock()
    c.search = MagicMock(return_value=[{"vod_name": "test"}])
    c.play = MagicMock(return_value=False)
    assert c._cmd_search_play(Command(C.MOVIE_SEARCH_PLAY, params={"query": "test"})) is False
    c.play.assert_called_once()
    texts = [call.args[0] for call in c._notify_voice.call_args_list]
    assert texts == ["正在加载：test", "电影加载未成功，请尝试其他片源"]


def test_music_command_wrapper_returns_same_result_as_manual_service():
    from app.music.controller import MusicController
    expected = {"title": "test"}
    service = SimpleNamespace(play_search_result=MagicMock(return_value=expected))
    assert MusicController._cmd_search_play(service, Command(C.MUSIC_SEARCH_PLAY,
        params={"query": "test"})) is expected
    service.play_search_result.assert_called_once_with("test", 0)


def test_empty_asr_stays_silent_and_does_not_consume_parameter_reply():
    rt = runtime()
    rt.asr.transcribe.return_value = ""
    rt.intent_router.parse("倍速")
    rt._process_speech(b"silence", 16000)
    assert rt.command_bus.get_history() == []
    rt._enqueue_speak.assert_not_called()
    assert rt.intent_router.parse("一点五倍").params["speed"] == 1.5


@pytest.mark.parametrize("text", ["我刚才说取消所有技能计时", "有人说帮我记石头人闪现", "比如这里有眼"])
def test_game_narration_never_becomes_a_write(text):
    assert IntentRouter({}).parse(text).intent_type == "system.unknown"


def test_cross_domain_parameter_reply_uses_latest_prompt_only():
    router = IntentRouter({})
    router.parse("电影音量")
    router.parse("倍速")
    assert router.parse("一点五倍").intent_type == C.MUSIC_SET_SPEED


def test_colloquial_flash_report_has_skill_and_side():
    intent = RuleBasedIntent.match("队友石头人交闪")
    assert intent.intent_type == C.COOLDOWN_START
    assert intent.params == {"champion": "石头人", "skill": "闪现", "side": "ally"}


@pytest.mark.parametrize("text", ["游戏，打开自动同意", "游戏，查询自动接受状态"])
def test_accept_aliases_are_supported_without_cross_domain_routing(text):
    intent = RuleBasedIntent.match(text)
    assert intent.intent_type in (C.AUTO_ACCEPT_SET, C.AUTO_STATUS_QUERY)


def test_callback_failure_does_not_retry_business_operation():
    bus = CommandBus()
    handler = MagicMock(return_value=True)
    callback = MagicMock(side_effect=RuntimeError("feedback unavailable"))
    bus.register(C.MUSIC_NEXT, handler)
    worker = bus.execute_async(Command(C.MUSIC_NEXT), on_done=callback)
    worker.join(3)
    assert not worker.is_alive()
    handler.assert_called_once()
    callback.assert_called_once()


def test_asr_generation_change_during_parse_is_checked_before_dispatch():
    rt = runtime()
    rt.asr.transcribe.return_value = "下一首"
    def parse(text):
        rt._asr_generation += 1
        return Intent(C.MUSIC_NEXT)
    rt.intent_router.parse = parse
    rt._process_speech(b"capture", 16000)
    assert rt.command_bus.get_history() == []


def test_boolean_cancel_all_result_is_not_misread_as_count():
    rt = runtime()
    rt.command_bus.register(C.COOLDOWN_CANCEL_ALL, lambda c: True)
    rt._execute_intent(Intent(C.COOLDOWN_CANCEL_ALL), "取消所有技能计时")
    assert "无法确认" in rt._enqueue_speak.call_args.args[0]
