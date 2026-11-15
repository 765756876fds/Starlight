"""Finite objectless vocabulary uses live arbitration; explicit domains remain explicit."""
import pytest

from app.core.command_bus import CommandType as C
from app.voice.intent import IntentRouter
from app.voice.runtime import VoiceRuntime
from tests.unit.test_media_context import context

PAUSE = ("停一下", "先停一下", "暂停一下", "暂停播放", "停一会儿", "稍微停一下", "先暂停")
RESUME = ("接着放", "继续播放", "恢复播放", "接着播", "继续放")


@pytest.mark.parametrize("text, action", [(s, "pause") for s in PAUSE] + [(s, "resume") for s in RESUME])
def test_context_aliases_share_one_action(text, action):
    result = IntentRouter({}).parse(text + "。")
    assert result.intent_type == C.MEDIA_CONTEXT and result.params == {"action": action}


@pytest.mark.parametrize("domain", ["music", "movie"])
@pytest.mark.parametrize("action, expressions", [("pause", PAUSE), ("resume", RESUME)])
def test_aliases_execute_same_live_domain(domain, action, expressions):
    for text in expressions:
        state = "playing" if action == "pause" else "paused"
        service, players, _, calls = context(**{domain: state, "movie" if domain == "music" else "music": "stopped"})
        runtime = VoiceRuntime({"asr": {}, "intent": {}})
        runtime.command_bus = service.runtime.command_bus
        runtime.media_context = service
        runtime._enqueue_speak = lambda *a, **k: None
        workers = []
        execute = runtime.command_bus.execute_async
        def tracked(*a, **kw):
            worker = execute(*a, **kw)
            workers.append(worker)
            return worker
        runtime.command_bus.execute_async = tracked
        runtime._execute_intent(runtime.intent_router.parse(text), text)
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
        assert calls == [(domain, action)], text
        assert players[domain].state == ("paused" if action == "pause" else "playing")


@pytest.mark.parametrize("music,movie", [("playing", "playing"), ("stopped", "stopped"), ("unknown", "playing")])
def test_ambiguous_or_unavailable_state_never_guesses(music, movie):
    for text in ("停一下", "先停一下", "继续播放", "接着放"):
        intent = IntentRouter({}).parse(text)
        assert intent.intent_type == C.MEDIA_CONTEXT
        service, _, _, calls = context(music, movie)
        service.execute(intent.params["action"])
        assert not calls


@pytest.mark.parametrize("text", ["不要停一下", "不要暂停播放", "不要继续播放", "为什么暂停播放？",
    "继续播放是什么意思", "他说停一下", "她说继续播放", "有人说接着播", '“停一下”',
    "先暂停电影再切歌", "暂停播放，然后继续播放", "这首歌不好听", "声音有点大", "石头人闪现用了是什么意思"])
def test_negative_questions_quotes_and_multiple_commands_do_not_control_media(text):
    intent = IntentRouter({}).parse(text)
    assert intent.intent_type not in (C.MEDIA_CONTEXT, C.MUSIC_PAUSE, C.MUSIC_RESUME, C.MOVIE_PAUSE, C.MOVIE_RESUME)


@pytest.mark.parametrize("text, expected", [("音乐暂停", C.MUSIC_PAUSE), ("电影暂停", C.MOVIE_PAUSE),
    ("音乐继续", C.MUSIC_RESUME), ("电影继续", C.MOVIE_RESUME), ("下一首", C.MUSIC_NEXT),
    ("下一集", C.MOVIE_NEXT_EPISODE), ("暂停", C.MUSIC_PAUSE), ("继续", C.MUSIC_RESUME),
    ("停止播放", "system.stop_tts"), ("别说了", "system.stop_tts")])
def test_explicit_and_interrupt_contracts_remain(text, expected):
    assert IntentRouter({}).parse(text).intent_type == expected


def test_movie_title_is_not_a_context_command():
    intent = IntentRouter({}).parse("播放电影继续播放")
    assert intent.intent_type == C.MOVIE_SEARCH_PLAY and intent.params["query"] == "继续播放"
