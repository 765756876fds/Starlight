"""Characterize reminders with a real bus and isolated in-memory game state."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.event_bus import EventBus, EventType
from app.core.notifications import NotificationCenter, operation_scope
from app.lol.game_alarm import AlarmStatus, GameAlarmEngine
from app.voice.intent import Intent
from app.voice.runtime import VoiceRuntime
from main import Application


@pytest.fixture
def chain(monkeypatch):
    bus = EventBus()
    center = NotificationCenter()
    monkeypatch.setattr("app.voice.runtime.get_notification_center", lambda: center)
    monkeypatch.setattr("app.core.notifications.get_notification_center", lambda: center)
    monkeypatch.setattr("app.lol.game_alarm.get_event_bus", lambda: bus)
    engine = GameAlarmEngine()
    app = SimpleNamespace(event_bus=bus, logger=Mock(),
                          lol=SimpleNamespace(game_alarm=engine, current_snapshot=None))
    Application._register_alarm_events(app)
    voice = VoiceRuntime({})
    voice.event_bus = bus
    voice.asr = Mock()
    voice.command_bus = Mock()
    voice._observations.submit = Mock()  # Passive diagnostic delivery is tested separately.
    try:
        yield voice, bus, app, engine, center
    finally:
        voice.stop()
        bus.shutdown()


def run_intent(voice, kind, params):
    return voice._execute_intent(Intent(kind, params=params), "测试提醒指令")


def said(bus):
    events = bus.get_history(EventType.SPEECH_SAY)
    assert all(e.source == "alarm_feedback" and e.get("priority") == "high" for e in events)
    return [e.get("text") for e in events]


@pytest.mark.parametrize("kind,params,event,data", [
    ("alarm.flash", {"champion": "石头人", "extra": 1}, "alarm.create_flash",
     {"champion": "石头人"}),
    ("alarm.spell", {"champion": "莎弥拉", "spell": "W"}, "alarm.create_spell",
     {"champion": "莎弥拉", "spell": "W"}),
    ("alarm.ward", {}, "alarm.create_ward", {"location": "当前位置"}),
    ("alarm.ward", {"location": "河道"}, "alarm.create_ward", {"location": "河道"}),
    ("alarm.custom", {}, "alarm.create_custom",
     {"duration": 120, "title": "自定义提醒", "content": ""}),
    ("alarm.custom", {"duration": 65, "title": "休息", "content": "喝水"}, "alarm.create_custom",
     {"duration": 65, "title": "休息", "content": "喝水"}),
    ("alarm.list", {"note": "keep"}, "alarm.list", {"note": "keep"}),
    ("alarm.remaining", {"note": "keep"}, "alarm.remaining", {"note": "keep"}),
    ("alarm.cancel_last", {"note": "keep"}, "alarm.cancel_last", {"note": "keep"}),
])
def test_publish_payload_and_final_voice_event(chain, kind, params, event, data):
    voice, bus, _, _, center = chain
    original = dict(params)
    assert run_intent(voice, kind, params) is None
    events = bus.get_history()
    assert events[0].event_type == event
    assert events[0].source == "voice"
    assert {k: v for k, v in events[0].data.items() if k != "timestamp"} == data
    assert events[-1].event_type == EventType.VOICE_COMMAND_EXECUTED
    assert events[-1].get("intent") == {"intent": kind, "confidence": 1.0, "params": original}
    assert params == original
    record, = center.history()
    assert (record.title, record.status, record.phase) == ("操作已发送，结果尚未确认", "warning", "done")
    assert voice.command_bus.mock_calls == []


@pytest.mark.parametrize("kind,params", [
    ("alarm.flash", {}),
    ("alarm.spell", {"champion": "莎弥拉"}),
    ("alarm.spell", {"spell": "W"}),
    ("alarm.other", {}),
])
def test_missing_or_unknown_reminder_only_emits_voice_event(chain, kind, params):
    voice, bus, _, engine, _ = chain
    assert run_intent(voice, kind, params) is None
    assert [e.event_type for e in bus.get_history()] == [EventType.VOICE_COMMAND_EXECUTED]
    assert not engine.get_user_memories()


@pytest.mark.parametrize("kind,params,feedback", [
    ("alarm.flash", {"champion": "石头人"}, ["没确认到目标"]),
    ("alarm.spell", {"champion": "莎弥拉", "spell": "W"}, ["没确认到目标"]),
    ("alarm.ward", {}, []),
    ("alarm.custom", {"duration": 10}, ["没有确认提醒时间"]),
    ("alarm.list", {}, ["当前没有记录"]),
    ("alarm.remaining", {}, ["当前没有记录"]),
    ("alarm.cancel_last", {}, ["当前没有记录"]),
])
def test_existing_feedback_without_lol_service(chain, kind, params, feedback):
    voice, bus, app, engine, _ = chain
    app.lol = None
    assert run_intent(voice, kind, params) is None
    assert said(bus) == feedback
    assert not engine.get_user_memories()


def test_successive_recordings_create_distinct_reminders_then_cancel_the_latest(chain):
    voice, bus, _, engine, _ = chain
    for audio, text in ((b"first", "十秒提醒我喝水"), (b"second", "十秒提醒我喝水")):
        voice.asr.transcribe.return_value = text
        voice._process_speech(audio, 16000)
    before = engine.get_user_memories()
    assert len(before) == 2 and before[0].alarm_id != before[1].alarm_id
    assert all(a.duration == 10 and a.time_base == "wall" and a.lead_time == 0
               and a.data["content"] == "喝水" for a in before)
    assert run_intent(voice, "alarm.cancel_last", {}) is None
    assert engine.get_user_memories() == before[:1]
    assert before[1].status is AlarmStatus.CANCELLED
    lifecycle = [e.event_type for e in bus.get_history()]
    assert lifecycle == ["alarm.create_custom", EventType.ALARM_CREATED, EventType.SPEECH_SAY,
                         EventType.VOICE_COMMAND_EXECUTED] * 2 + [
                             "alarm.cancel_last", EventType.ALARM_CANCELLED, EventType.SPEECH_SAY,
                             EventType.VOICE_COMMAND_EXECUTED]
    assert said(bus) == ["记好了，10秒后提醒你喝水"] * 2 + ["已取消：十秒提醒"]


def test_subscriber_failure_keeps_original_error_event_and_next_request_runs(chain, monkeypatch):
    voice, bus, _, engine, _ = chain
    original = engine.create_custom_alarm
    monkeypatch.setattr(engine, "create_custom_alarm", Mock(side_effect=RuntimeError("test failure")))
    assert run_intent(voice, "alarm.custom", {"duration": 10}) is None
    assert [e.event_type for e in bus.get_history()] == [
        "alarm.create_custom", EventType.SYSTEM_ERROR, EventType.VOICE_COMMAND_EXECUTED]
    assert not engine.get_user_memories()
    assert not said(bus)

    monkeypatch.setattr(engine, "create_custom_alarm", original)
    assert run_intent(voice, "alarm.custom", {"duration": 10}) is None
    assert len(engine.get_user_memories()) == 1
    assert said(bus) == ["记好了，到点提醒你"]


def test_overdue_input_stops_before_reminder_dispatch(chain, monkeypatch):
    voice, bus, _, engine, center = chain
    now = [100.0]
    monkeypatch.setattr("app.voice.runtime.time", SimpleNamespace(monotonic=lambda: now[0]))
    parse = voice.intent_router.parse

    def late_parse(text):
        intent = parse(text)
        now[0] += 8.1
        return intent

    monkeypatch.setattr(voice.intent_router, "parse", late_parse)
    voice.asr.transcribe.return_value = "十秒提醒我喝水"
    operation = center.post("测试")
    with operation_scope(operation):
        voice._process_speech(b"old-recording", 16000)
    assert not bus.get_history()
    assert not engine.get_user_memories()
    assert center.get(operation).title == "这条指令等待过久，请再说一次"


@pytest.mark.parametrize("restart", [False, True])
def test_stopped_session_cannot_publish_late_input(chain, restart):
    voice, bus, _, engine, _ = chain
    started, release = threading.Event(), threading.Event()

    def transcribe(audio, _):
        if audio == b"old":
            started.set()
            assert release.wait(4)
            return "十秒提醒我喝水"
        return "五秒提醒我休息"

    voice.asr.transcribe.side_effect = transcribe
    voice.start("disabled")
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(voice._process_speech, b"old", 16000)
        try:
            assert started.wait(2)
            voice.stop()
            if restart:
                voice.start("disabled")  # No microphone, model or service startup.
                voice._process_speech(b"new", 16000)
        finally:
            release.set()
        old.result(timeout=2)
    reminders = engine.get_user_memories()
    assert [a.data["content"] for a in reminders] == (["休息"] if restart else [])
    assert len(bus.get_history("alarm.create_custom")) == int(restart)
    assert len(bus.get_history(EventType.VOICE_COMMAND_EXECUTED)) == int(restart)

def test_dispatch_contract_in_isolation():
    from app.voice.alarm_dispatch import dispatch_alarm_intent

    bus = Mock(spec=EventBus)
    assert dispatch_alarm_intent(bus, "alarm.spell", {"champion": "莎弥拉"}) is None
    bus.publish.assert_not_called()
    assert dispatch_alarm_intent(bus, "alarm.ward", {}) is None
    bus.publish.assert_called_once()
    bus.publish_async.assert_not_called()
    event = bus.publish.call_args.args[0]
    assert (event.event_type, event.source, event.get("location")) == (
        "alarm.create_ward", "voice", "当前位置")


def test_flash_and_spell_preserve_cooldowns_and_existing_query_feedback(chain):
    voice, bus, _, engine, _ = chain
    engine._current_game_time = 100
    run_intent(voice, "alarm.flash", {"champion": "石头人"})
    run_intent(voice, "alarm.spell", {"champion": "莎弥拉", "spell": "W"})
    flash, spell = engine.get_user_memories()
    assert flash.duration == 300.0 and spell.duration == 60.0
    assert flash.time_base == spell.time_base == "game"
    engine._current_game_time = 110
    run_intent(voice, "alarm.list", {})
    run_intent(voice, "alarm.remaining", {})
    assert said(bus) == ["记好了，熔岩巨兽闪现", "记好了，莎弥拉 W",
                         f"记了 2 个：{flash.title}、{spell.title}",
                         f"{spell.title}，还有 0 分 50 秒"]


def test_already_created_game_reminder_can_fire_after_voice_stop(chain):
    voice, bus, _, engine, _ = chain
    voice.start("disabled")
    assert run_intent(voice, "alarm.custom", {"duration": 10}) is None
    reminder, = engine.get_user_memories()
    voice.stop()
    reminder.game_time_start -= 11
    engine._check_wall_alarms()
    assert reminder.status is AlarmStatus.TRIGGERED
    triggered, = bus.get_history(EventType.ALARM_TRIGGERED)
    assert triggered.get("alarm")["alarm_id"] == reminder.alarm_id
