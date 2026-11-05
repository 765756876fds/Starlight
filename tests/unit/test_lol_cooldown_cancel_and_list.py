"""Cancellation and live timer queries through the real manager and voice bus."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.command_bus import Command, CommandBus, CommandType as C
from app.lol.cooldown.events import COOLDOWN_CANCELLED, COOLDOWN_READY, COOLDOWN_REMIND
from app.lol.cooldown.manager import CooldownManager
from app.lol.cooldown.models import ReminderStatus
from app.voice.runtime import VoiceRuntime

pytestmark = pytest.mark.unit


@pytest.fixture
def timers():
    events, speeches = [], []
    manager = CooldownManager(
        scheduler=Mock(),
        on_event=lambda code, data: events.append((code, data)),
        speech_enqueue=lambda text, event_id: speeches.append((text, event_id)),
    )
    manager.set_game_time(1000.0)
    yield SimpleNamespace(manager=manager, events=events, speeches=speeches)
    manager.cancel_all()


def listed_ids(manager):
    return {item["id"] for item in manager.handle_list()}


@pytest.fixture
def voice(timers):
    runtime = VoiceRuntime({"asr": {}, "intent": {}})
    runtime.command_bus = CommandBus()
    runtime.event_bus = Mock()
    runtime._enqueue_speak = Mock()
    for kind, handler in (
        (C.COOLDOWN_START, timers.manager.handle_start),
        (C.COOLDOWN_CANCEL, timers.manager.handle_cancel),
        (C.COOLDOWN_CANCEL_ALL, timers.manager.handle_cancel_all),
        (C.COOLDOWN_LIST, timers.manager.handle_list),
    ):
        runtime.command_bus.register(kind, handler)
    return runtime


def say(runtime, text):
    intent = runtime.intent_router.parse(text)
    runtime._execute_intent(intent, text)
    return intent


@pytest.mark.parametrize("created,cancelled", [
    ("石头人", "石头人"), ("石头人", "墨菲特"), ("墨菲特", "石头人"),
])
def test_voice_alias_cancel_removes_the_created_timer(timers, voice, created, cancelled):
    other = timers.manager.start_reminder("亚索", "闪现")
    assert say(voice, f"{created}闪现用了").intent_type == C.COOLDOWN_START
    target, = [r for r in timers.manager.list_active() if r.id != other.id]
    assert target.champion_name == "墨菲特"
    voice._enqueue_speak.assert_called_with("技能记录成功", "voice_cooldown")

    assert say(voice, f"取消{cancelled}计时").intent_type == C.COOLDOWN_CANCEL
    voice._enqueue_speak.assert_called_with("技能计时已取消", "voice_cooldown")
    assert timers.manager.get(target.id) is None
    assert listed_ids(timers.manager) == {other.id}
    assert [data["id"] for code, data in timers.events if code == COOLDOWN_CANCELLED] == [target.id]


@pytest.mark.parametrize("champion", ["不存在的英雄", "", None])
def test_unresolved_cancel_does_not_cancel_any_timer(timers, champion):
    reminder = timers.manager.start_reminder("墨菲特", "闪现")
    assert timers.manager.handle_cancel(Command(C.COOLDOWN_CANCEL, {"champion": champion})) is False
    assert listed_ids(timers.manager) == {reminder.id}


@pytest.mark.parametrize("params", [
    {"skill": "闪现", "side": "enemy"},
    {"spell": "Flash", "side": "enemy"},
    {"skill": "r", "side": "ally"},
    {"skill": "大招", "side": "ally"},
])
def test_alias_cancel_respects_explicit_skill_and_side(timers, params):
    manager = timers.manager
    enemy_flash = manager.start_reminder("石头人", "闪现", side="enemy")
    ally_flash = manager.start_reminder("墨菲特", "闪现", side="ally")
    ally_r = manager.start_reminder("墨菲特", "R", side="ally")
    other = manager.start_reminder("亚索", "闪现", side="enemy")
    target = ally_r if params.get("skill") in ("r", "大招") else enemy_flash
    assert manager.handle_cancel(Command(C.COOLDOWN_CANCEL, {"champion": "石头人", **params})) is True
    assert listed_ids(manager) == {enemy_flash.id, ally_flash.id, ally_r.id, other.id} - {target.id}


def test_unknown_skill_filter_cannot_broaden_alias_cancellation(timers):
    reminder = timers.manager.start_reminder("石头人", "闪现")
    assert timers.manager.handle_cancel({"champion": "石头人", "skill": "不存在的技能"}) is False
    assert listed_ids(timers.manager) == {reminder.id}


@pytest.mark.parametrize("status", list(ReminderStatus))
def test_query_only_includes_live_active_and_reminded(timers, status):
    reminder = timers.manager.start_reminder("石头人", "闪现")
    reminder.status = status.value
    expected = {reminder.id} if status in (ReminderStatus.ACTIVE, ReminderStatus.REMINDED) else set()
    assert listed_ids(timers.manager) == expected


@pytest.mark.parametrize("status", [ReminderStatus.ACTIVE, ReminderStatus.REMINDED])
@pytest.mark.parametrize("remaining", [0.1, 0.0, -1.0])
def test_query_uses_game_time_even_before_expiry_cleanup(timers, monkeypatch, status, remaining):
    reminder = timers.manager.start_reminder("石头人", "闪现")
    reminder.status = status.value
    # Simulate a clock update observed before the cleanup tick has removed the record.
    monkeypatch.setattr(timers.manager, "_game_time", reminder.ready_at - remaining)
    assert timers.manager.get(reminder.id) is reminder
    assert listed_ids(timers.manager) == ({reminder.id} if remaining > 0 else set())


def test_reminded_voice_query_reports_timer_until_ready_without_repeating_speech(timers, voice):
    manager = timers.manager
    say(voice, "石头人闪现用了")
    reminder, = manager.list_active()
    say(voice, "查询当前技能计时")
    voice._enqueue_speak.assert_called_with("当前有1个技能计时", "voice_cooldown")
    manager.set_game_time(reminder.remind_at - 0.1)
    assert timers.speeches == []

    manager.set_game_time(reminder.remind_at)
    assert reminder.status == ReminderStatus.REMINDED.value
    assert len(timers.speeches) == 1
    manager.set_game_time(reminder.remind_at + 1)
    assert reminder.ready_at - manager.current_game_time() == 29.0
    assert listed_ids(manager) == {reminder.id}
    say(voice, "查询当前技能计时")
    voice._enqueue_speak.assert_called_with("当前有1个技能计时", "voice_cooldown")
    assert manager.list_active() == []  # Keep its ACTIVE-only contract for adjustment callers.

    manager.set_game_time(reminder.remind_at + 1)
    manager.set_game_time(reminder.ready_at)
    manager.set_game_time(reminder.ready_at + 1)
    say(voice, "查询当前技能计时")
    voice._enqueue_speak.assert_called_with("当前没有正在进行的技能计时", "voice_cooldown")
    assert manager.get(reminder.id) is None
    assert reminder.status == ReminderStatus.EXPIRED.value
    assert len(timers.speeches) == 1
    assert sum(code == COOLDOWN_REMIND for code, _ in timers.events) == 1
    assert sum(code == COOLDOWN_READY for code, _ in timers.events) == 1


def test_cancel_reminded_timer_cannot_reappear_or_fire_ready(timers, voice):
    manager = timers.manager
    reminder = manager.start_reminder("墨菲特", "闪现")
    manager.set_game_time(reminder.remind_at)
    say(voice, "取消石头人计时")
    voice._enqueue_speak.assert_called_with("技能计时已取消", "voice_cooldown")
    assert manager.get(reminder.id) is None
    manager.set_game_time(reminder.ready_at + 1)
    assert manager.handle_list() == []
    assert len(timers.speeches) == 1
    assert not any(code == COOLDOWN_READY for code, _ in timers.events)


def test_cancel_all_still_clears_active_and_reminded_with_real_feedback(timers, voice):
    flash = timers.manager.start_reminder("石头人", "闪现")
    timers.manager.set_game_time(flash.remind_at)
    timers.manager.start_reminder("亚索", "闪现")
    say(voice, "取消所有技能计时")
    voice._enqueue_speak.assert_called_once_with("已取消全部技能计时，共2个", "voice_cooldown")
    assert timers.manager.handle_list() == []
    assert timers.manager.get_active_reminders() == []


def test_game_end_cleanup_keeps_clock_unsynced_and_timers_gone(timers):
    reminder = timers.manager.start_reminder("石头人", "闪现")
    timers.manager.set_game_time(reminder.remind_at)
    # These are the two handlers registered for game.ended in main.py.
    timers.manager.reset_clock()
    timers.manager.clear()
    assert timers.manager.handle_list() == []
    assert timers.manager.get(reminder.id) is None
    assert timers.manager.start_reminder("石头人", "闪现") is None
    timers.manager.set_game_time(reminder.ready_at + 1)
    assert timers.manager.handle_list() == []
    assert not any(code == COOLDOWN_READY for code, _ in timers.events)


@pytest.mark.parametrize("text", [
    "石头人闪现用了是什么意思", "怎么取消所有技能计时", "不要取消所有技能计时",
])
def test_voice_questions_and_negation_do_not_touch_real_timers(timers, voice, text):
    reminder = timers.manager.start_reminder("亚索", "闪现")
    intent = say(voice, text)
    assert intent.intent_type == "system.unknown"
    assert voice.command_bus.get_history() == []
    assert listed_ids(timers.manager) == {reminder.id}


def test_voice_missing_champion_does_not_guess_or_report_success(timers, voice):
    assert say(voice, "对面W交了").intent_type == C.COOLDOWN_START
    assert timers.manager.handle_list() == []
    voice._enqueue_speak.assert_called_once_with("技能记录未成功", "voice_cooldown")
