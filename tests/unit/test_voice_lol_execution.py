"""Real ASR-result -> router -> bus -> timer tests, without audio devices."""
from unittest.mock import Mock

import pytest

from app.core.command_bus import CommandBus, CommandType as C
from app.lol.cooldown.manager import CooldownManager
from app.voice.runtime import VoiceRuntime

pytestmark = pytest.mark.unit


@pytest.fixture
def chain():
    manager = CooldownManager(scheduler=Mock())
    manager.set_game_time(1000)
    runtime = VoiceRuntime({"asr": {}, "intent": {}})
    runtime.asr = Mock()
    runtime.command_bus = CommandBus()
    runtime.event_bus = Mock()
    runtime._enqueue_speak = Mock()
    for kind, handler in ((C.COOLDOWN_START, manager.handle_start),
                          (C.COOLDOWN_CANCEL, manager.handle_cancel)):
        runtime.command_bus.register(kind, handler)
    yield runtime, manager
    manager.cancel_all()


def recognize(runtime, text, audio):
    runtime.asr.transcribe.return_value = text
    runtime._process_speech(audio, 16000)


@pytest.mark.parametrize("hero", ["石头人", "墨菲特"])
def test_independent_asr_commands_create_then_cancel(chain, hero):
    runtime, manager = chain
    recognize(runtime, f"{hero}闪现用了。", b"first-recording")
    reminder, = manager.list_active()
    assert reminder.champion_name == "墨菲特"
    runtime._enqueue_speak.assert_called_with("技能记录成功", "voice_cooldown")
    recognize(runtime, f"取消{hero}计时。", b"second-recording")
    runtime._enqueue_speak.assert_called_with("技能计时已取消", "voice_cooldown")
    assert manager.get(reminder.id) is None
    assert [c.command_type for c in runtime.command_bus.get_history()] == [C.COOLDOWN_START, C.COOLDOWN_CANCEL]


@pytest.mark.parametrize("text", [
    "石头人闪现用了，石头人闪现用了。",
    "取消石头人计时，取消石头人计时。",
])
def test_comma_repeated_asr_clause_executes_only_once(chain, text):
    runtime, manager = chain
    cancelling = text.startswith("取消")
    if cancelling:
        manager.start_reminder("墨菲特", "闪现")
    recognize(runtime, text, b"one-recording-with-repeated-transcript")
    assert len(runtime.command_bus.get_history()) == 1
    assert len(manager.handle_list()) == (0 if cancelling else 1)


@pytest.mark.parametrize("text", ["石头闪现用了。", "土豆儿惩戒用了。", "对面W交了。"])
def test_unresolved_or_missing_champion_never_guesses_a_timer(chain, text):
    runtime, manager = chain
    recognize(runtime, text, b"incomplete-or-misheard")
    assert manager.handle_list() == []
    runtime._enqueue_speak.assert_called_with("技能记录未成功", "voice_cooldown")


@pytest.mark.parametrize("text", [
    "石头人闪现用了是什么意思", "怎么取消所有技能计时", "不要取消所有技能计时",
    "石头人闪现用了，不要取消石头人计时",
])
def test_questions_negation_and_mixed_clauses_do_not_write(chain, text):
    runtime, manager = chain
    recognize(runtime, text, b"safe-counterexample")
    assert runtime.command_bus.get_history() == []
    assert manager.handle_list() == []


def test_business_failure_is_not_success_and_next_command_still_runs(chain):
    runtime, manager = chain
    manager.reset_clock()
    recognize(runtime, "石头人闪现用了", b"unsynced-command")
    runtime._enqueue_speak.assert_called_with("技能记录未成功", "voice_cooldown")
    manager.set_game_time(1000)
    recognize(runtime, "墨菲特闪现用了", b"new-command")
    runtime._enqueue_speak.assert_called_with("技能记录成功", "voice_cooldown")
    assert len(manager.handle_list()) == 1


@pytest.mark.parametrize("text", [
    "'石头人闪现用了,石头人闪现用了'",
    '\"取消石头人计时,取消石头人计时\"',
    "“石头人闪现用了，石头人闪现用了”",
    "（石头人闪现用了,石头人闪现用了）",
    "《取消石头人计时,取消石头人计时》",
])
def test_repeated_quoted_commands_preserve_quote_safety(chain, text):
    runtime, manager = chain
    reminder = manager.start_reminder("石头人", "闪现")
    recognize(runtime, text, b"quoted-repeated-command")
    assert runtime.command_bus.get_history() == []
    assert {r["id"] for r in manager.handle_list()} == {reminder.id}
