"""Text shadow corpus and real router/runtime/bus, without devices or HTTP."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.command_bus import CommandBus, CommandType as C
from app.voice.intent import IntentRouter, RuleBasedIntent
from app.voice.runtime import VoiceRuntime


POSITIVE = [
    ("暂停音乐", C.MUSIC_PAUSE),
    ("音乐暂停", C.MUSIC_PAUSE),
    ("音乐先别放了", C.MUSIC_PAUSE),
    ("把音乐先停一下", C.MUSIC_PAUSE),
    ("这首歌先暂停一下", C.MUSIC_PAUSE),
    ("歌曲先别放了", C.MUSIC_PAUSE),
    ("先暂停一下音乐", C.MUSIC_PAUSE),
    ("音乐接着放", C.MUSIC_RESUME),
    ("接着放音乐", C.MUSIC_RESUME),
    ("这首歌接着听", C.MUSIC_RESUME),
    ("继续放歌曲", C.MUSIC_RESUME),
    ("继续音乐", C.MUSIC_RESUME),
    ("换首歌", C.MUSIC_NEXT),
    ("切下一首", C.MUSIC_NEXT),
    ("帮我换一首歌", C.MUSIC_NEXT),
    ("音乐切到下一首", C.MUSIC_NEXT),
    ("上一首歌", C.MUSIC_PREV),
    ("音乐切回上一首", C.MUSIC_PREV),
    ("换回上一首歌", C.MUSIC_PREV),
    ("请把音乐先停一下。", C.MUSIC_PAUSE),
]
CONTEXTUAL = ["先停一下", "停一下", "别放了", "接着放", "接着播放", "接着听"]
NEGATIVE = [
    "这首歌不好听", "声音有点大", "不要暂停音乐", "为什么要暂停音乐？",
    "他说让我暂停音乐", "先暂停电影再切歌", "音乐先别放了是什么意思",
    "不要停止播放", "他说别放了", "为什么别放了", "不要接着放音乐",
    "我刚才说音乐先别放了", "音乐先别放了吗", "音乐先别放了好不好",
    "音乐先", "接着", "音乐音乐先别放了", "先停一下先停一下",
    "暂停电影再音乐接着放", "音乐先别放了然后接着放音乐",
    "音乐先别放了，切下一首", "音乐和电影先别放了", "电影接着放",
    "音乐切到下一首上一首", "音乐先别放了？", "“音乐先别放了”",
    "音乐先别，放了", "接着放音乐，为什么", "下一首？",
]


@pytest.mark.parametrize("text,expected", POSITIVE)
def test_shadow_positive(text, expected):
    intent = IntentRouter({}).parse(text)
    assert intent.intent_type == expected
    assert intent.params == {}
    if expected in (C.MUSIC_PAUSE, C.MUSIC_RESUME):
        assert intent.scope == "music"


@pytest.mark.parametrize("text", NEGATIVE)
def test_shadow_negative_never_executes(text):
    assert IntentRouter({}).parse(text).intent_type in ("system.unknown", "system.clarify")


@pytest.mark.parametrize("text", CONTEXTUAL)
def test_no_object_has_no_reliable_state_and_never_guesses(text):
    router = IntentRouter({})
    # No stale utterance context may stand in for live music/movie state.
    router.parse("暂停音乐")
    assert router.parse(text).intent_type == "system.clarify"
    router.parse("继续电影")
    assert router.parse(text).intent_type == "system.clarify"


@pytest.mark.parametrize("text,expected", POSITIVE)
def test_narration_questions_negation_and_quotes_around_each_command(text, expected):
    for wrapped in (f"他说{text}", f"为什么{text}？", f"不要{text}",
                    f"‘{text}’", f"{text}是什么意思", f"{text}然后切下一首"):
        intent = IntentRouter({}).parse(wrapped)
        assert intent.intent_type in ("system.unknown", "system.clarify"), wrapped


@pytest.mark.parametrize("text,expected", POSITIVE)
def test_real_runtime_dispatches_only_music_once(text, expected):
    rt = VoiceRuntime({"asr": {}, "intent": {}})
    rt.command_bus = CommandBus()
    rt._enqueue_speak = Mock()
    rt._real_paused = Mock(return_value=expected == C.MUSIC_PAUSE)
    handler = Mock(return_value=True)
    rt.command_bus.register(expected, handler)
    rt._execute_intent(rt.intent_router.parse(text), text)
    handler.assert_called_once()
    assert handler.call_args.args[0].command_type == expected
    assert len(rt.command_bus.get_history()) == 1


def test_unavailable_context_cannot_touch_either_player():
    rt = VoiceRuntime({"asr": {}, "intent": {}})
    rt.command_bus = CommandBus()
    rt._enqueue_speak = Mock()
    for text in CONTEXTUAL + NEGATIVE:
        rt._execute_intent(rt.intent_router.parse(text), text)
    assert rt.command_bus.get_history() == []


def test_explicit_rules_and_parameter_followup_keep_priority():
    router = IntentRouter({})
    for text in ("暂停", "继续", "暂停电影", "播放不要说话", "石头人闪现用了"):
        old = RuleBasedIntent.match(text)
        new = router.parse(text)
        assert (new.intent_type, new.params, new.scope) == (old.intent_type, old.params, old.scope)
    assert router.parse("倍速").params == {}
    assert router.parse("一点五倍").params == {"speed": 1.5}
    router.parse("倍速")
    assert router.parse("音乐接着放").intent_type == C.MUSIC_RESUME
    assert router.parse("一点五倍").intent_type == "system.unknown"


@pytest.mark.parametrize("text", [
    "＂下一首＂", "＇音乐先别放了＇", "音乐接着放？",
    "《音乐先别放了》", "【下一首】", "(音乐接着放)",
])
def test_raw_punctuation_is_checked_before_normalization(text):
    assert IntentRouter({}).parse(text).intent_type in ("system.unknown", "system.clarify")


@pytest.mark.parametrize("text,command,paused", [
    ("音乐先别放了", C.MUSIC_PAUSE, True),
    ("音乐接着放", C.MUSIC_RESUME, False),
])
def test_real_ipc_confirmation_path_never_queries_or_controls_movie(text, command, paused):
    rt = VoiceRuntime({"asr": {}, "intent": {}})
    rt.command_bus = CommandBus()
    rt._enqueue_speak = Mock()
    state = {"pause": not paused}
    rt.music_mpv_provider = lambda: SimpleNamespace(
        is_running=lambda: True, get_property=lambda name, **kwargs: state[name])
    rt.movie_mpv_provider = Mock(side_effect=AssertionError("must not query movie"))
    calls = []

    def control(cmd):
        calls.append(cmd.command_type)
        state["pause"] = paused
        return True

    rt.command_bus.register(command, control)
    rt._execute_intent(rt.intent_router.parse(text), text)
    assert calls == [command]
    assert state["pause"] is paused
    rt.movie_mpv_provider.assert_not_called()
    assert rt._enqueue_speak.call_args.args[0] == ("音乐已暂停" if paused else "音乐已继续")
