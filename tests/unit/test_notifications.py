"""Business facts, correlated speech, bounded history; no devices or network."""
from unittest.mock import Mock
import time

import pytest

from app.core import notifications as module
from app.core.command_bus import CommandBus, Command, CommandType as C
from app.core.notifications import NotificationCenter, operation_scope, notify
from app.speech.speech_queue import SpeechItem, SpeechQueue, PRIORITY_A, PRIORITY_B
from app.voice.runtime import VoiceRuntime
from app.voice.intent import Intent


@pytest.fixture
def center(monkeypatch):
    value = NotificationCenter()
    monkeypatch.setattr(module, "_center", value)
    return value


def voice():
    runtime = VoiceRuntime({})
    runtime.command_bus = CommandBus()
    runtime.event_bus = Mock()
    return runtime


def test_recognition_execution_and_failed_speech_share_one_success_record(center):
    runtime = voice()
    calls = []
    runtime.command_bus.register(C.MUSIC_PAUSE, lambda command: calls.append(command) or True)
    player = Mock()
    player.is_running.return_value = True
    player.get_property.return_value = True
    runtime.music_mpv_provider = lambda: player
    tts = Mock()
    tts.speak.return_value = False
    queue = SpeechQueue(tts)
    runtime.speech_queue_provider = lambda: queue
    operation_id = center.post("正在识别", source="voice", phase="recognizing")
    intent = Intent(C.MUSIC_PAUSE, params={})
    intent.scope = "music"
    with operation_scope(operation_id):
        runtime._execute_intent(intent, "暂停音乐")
    queue._running = True
    queue._worker_iteration()
    item, = center.history()
    assert item.id == operation_id
    assert item.heard == "暂停音乐"
    assert item.title == "音乐已暂停"
    assert item.status == "success"
    assert item.speech_status == "failed"
    assert len(calls) == 1, "播报失败不能重做业务"
    assert queue.is_empty()


@pytest.mark.parametrize("text", ["怎么取消所有技能计时", "不要取消所有技能计时",
                                "石头人闪现用了是什么意思", "'打开自动排队'"])
def test_questions_negation_and_quotes_do_not_execute(center, text):
    runtime = voice()
    runtime.asr = Mock(transcribe=Mock(return_value=text))
    runtime._process_speech(text.encode(), 16000)
    assert runtime.command_bus.get_history() == []
    item, = center.history()
    assert item.status == "warning"
    assert item.heard == text
    assert "Intent" not in item.message


def test_unknown_command_has_friendly_feedback(center):
    runtime = voice()
    runtime._execute_intent(Intent("system.unknown", params={}), "帮我改变世界")
    record, = center.history()
    assert record.status == "warning"
    assert "没有理解" in record.title
    assert "请" in record.advice


def test_real_handler_false_does_not_become_success(center):
    runtime = voice()
    runtime.command_bus.register(C.MUSIC_SET_SPEED, lambda _: False)
    runtime._execute_intent(Intent(C.MUSIC_SET_SPEED, params={"speed": 1.5}), "音乐一点五倍速")
    record, = center.history()
    assert record.status == "failed"
    assert "未" in record.title


def test_legacy_none_is_not_success(center):
    runtime = voice()
    runtime.command_bus.register(C.MUSIC_NEXT, lambda _: None)
    runtime._execute_intent(Intent(C.MUSIC_NEXT, params={}), "下一首")
    record, = center.history()
    assert record.status == "warning"
    assert "确认" in record.message


def test_old_result_updates_its_history_but_not_the_current_operation(center):
    old = center.post("旧搜索", source="voice")
    new = center.post("新指令", source="voice")
    with operation_scope(old):
        notify("旧搜索完成")
    assert center.current_voice_id == new
    assert [item.id for item in center.history()] == [new, old]
    assert center.get(old).status == "success"


def test_cleared_operations_are_not_resurrected_by_late_callbacks(center):
    operation_id = center.post("正在搜索", source="voice")
    center.clear()
    with operation_scope(operation_id):
        notify("晚到的结果")
    center.update(operation_id, speech_status="failed")
    assert center.history() == ()


def test_history_is_bounded_and_newest_first():
    center = NotificationCenter(limit=3)
    for index in range(10):
        center.post(str(index))
    assert [row.title for row in center.history()] == ["9", "8", "7"]


def test_duplicate_failures_do_not_flood_history(center):
    for _ in range(20):
        notify("语音播报未成功", status="warning", dedup_key="speech")
    assert len(center.history()) == 1


@pytest.mark.parametrize("raw", ["Intent(conf=0.9, params={})", "Traceback: private data",
                                 "handler failed", "CommandBus failed", "IPC error: test"])
def test_technical_messages_are_not_user_notifications(center, raw):
    notify("操作没有完成", raw, status="failed")
    item, = center.history()
    assert item.message == "操作没有完成，请重试或查看设置中的诊断结果"
    assert raw not in item.message


def test_speech_expiration_does_not_cancel_an_independent_game_reminder(center):
    runtime = voice()
    queue = SpeechQueue(Mock())
    queue._wait_for_playback_with_interrupt_check = Mock()
    runtime.speech_queue_provider = lambda: queue
    old = center.post("旧操作", source="voice")
    with operation_scope(old):
        runtime._enqueue_speak("旧反馈")
    runtime._voice_request_generation += 1
    game = SpeechItem("闪现即将就绪", PRIORITY_A, time.time() + 30, category="game")
    queue.enqueue(game)
    queue._running = True
    queue._worker_iteration()
    queue._worker_iteration()
    queue.tts.speak.assert_called_once_with("闪现即将就绪")
    assert game.notification_id == ""
    assert game.status == "finished"
    assert center.get(old).speech_status == "cancelled"


def test_command_scope_is_carried_into_async_execution(center):
    import threading
    bus = CommandBus()
    bus.register(C.MUSIC_SET_SPEED, lambda command: True)
    operation_id = center.post("正在设置", source="voice")
    with operation_scope(operation_id):
        command = Command(C.MUSIC_SET_SPEED, params={"speed": 1.5})
    thread = bus.execute_async(command)
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert command.notification_id == operation_id
    assert center.get(operation_id).status == "success"


def test_duplicate_audio_is_not_a_second_pending_notification(center):
    runtime = voice()
    runtime._running = True
    runtime.asr = Mock(transcribe=Mock(return_value="下一首"))
    handler = Mock(return_value=None)
    runtime.command_bus.register(C.MUSIC_NEXT, handler)
    runtime._speech_queue.put((b"same-audio", 16000))
    runtime._speech_queue.put((b"same-audio", 16000))
    runtime._speech_queue.put(None)
    runtime._asr_loop(0)
    assert len(center.history()) == 1
    assert center.history()[0].phase == "done"
    handler.assert_called_once()


def test_failed_speech_does_not_block_the_next_item_or_change_business(center):
    tts = Mock()
    tts.speak.side_effect = [TimeoutError(), True]
    queue = SpeechQueue(tts)
    queue._wait_for_playback_with_interrupt_check = Mock()
    keys = []
    for title in ["音乐已暂停", "技能计时已取消"]:
        key = center.post(title, status="success", source="voice", phase="done")
        keys.append(key)
        with operation_scope(key):
            queue.enqueue(SpeechItem(title, PRIORITY_B, time.time() + 30))
    queue._running = True
    queue._worker_iteration()
    queue._worker_iteration()
    assert center.get(keys[0]).status == "success"
    assert center.get(keys[0]).speech_status == "failed"
    assert center.get(keys[1]).status == "success"
    assert center.get(keys[1]).speech_status == "finished"
    assert tts.speak.call_count == 2
    assert queue.is_empty()


def test_broken_speech_provider_is_visible_without_reexecuting_business(center):
    runtime = voice()
    runtime.speech_queue_provider = Mock(side_effect=RuntimeError("queue unavailable"))
    handler = Mock(return_value=True)
    runtime.command_bus.register(C.MUSIC_SET_SPEED, handler)
    runtime._execute_intent(Intent(C.MUSIC_SET_SPEED, params={"speed": 1.5}), "音乐一点五倍速")
    record, = center.history()
    assert record.status == "success"
    # Speed's controller owns normal TTS; observe a response through the same operation.
    with operation_scope(record.id):
        runtime._enqueue_speak("音乐倍速已设置")
    assert center.get(record.id).speech_status == "failed"
    handler.assert_called_once()


@pytest.mark.parametrize("actual, status", [(True, "success"), (False, "failed"), (None, "warning")])
def test_movie_pause_notification_uses_real_player_state(center, actual, status):
    runtime = voice()
    runtime.command_bus.register(C.MOVIE_PAUSE, lambda command: True)
    player = Mock()
    player.is_running.return_value = True
    player.get_property.return_value = actual
    runtime.movie_mpv_provider = lambda: player
    runtime._execute_intent(Intent(C.MOVIE_PAUSE, params={}), "暂停电影")
    item, = center.history()
    assert item.status == status
    assert "电影" in item.title


def test_real_cooldown_alias_create_cancel_and_missing_query_are_reported(center):
    from app.lol.cooldown.manager import CooldownManager
    manager = CooldownManager(scheduler=Mock())
    manager.set_game_time(1000)
    runtime = voice()
    for command, handler in [(C.COOLDOWN_START, manager.handle_start),
                             (C.COOLDOWN_CANCEL, manager.handle_cancel),
                             (C.COOLDOWN_QUERY, manager.handle_query)]:
        runtime.command_bus.register(command, handler)
    try:
        runtime._execute_intent(Intent(C.COOLDOWN_START, params={"champion": "石头人", "skill": "闪现"}), "石头人闪现用了")
        assert center.history()[0].status == "success"
        runtime._execute_intent(Intent(C.COOLDOWN_CANCEL, params={"champion": "墨菲特"}), "取消墨菲特计时")
        assert center.history()[0].status == "success"
        assert manager.handle_list() == []
        runtime._execute_intent(Intent(C.COOLDOWN_QUERY, params={"champion": "不存在", "skill": "Q"}), "查询技能")
        assert center.history()[0].status == "warning"
        assert "没有找到" in center.history()[0].message
    finally:
        manager.cancel_all()


def test_notification_observer_failure_cannot_change_business_return(center, monkeypatch):
    bus = CommandBus()
    handler = Mock(return_value=True)
    bus.register(C.MUSIC_SET_SPEED, handler)
    operation_id = center.post("正在设置倍速", source="voice")
    monkeypatch.setattr(center, "update", Mock(side_effect=RuntimeError("observer unavailable")))
    with operation_scope(operation_id):
        assert bus.execute(Command(C.MUSIC_SET_SPEED)) is True
    handler.assert_called_once()


def test_intentionally_disabled_tts_is_not_reported_as_a_network_failure(center):
    tts = Mock(enabled=False)
    tts.speak.return_value = False
    queue = SpeechQueue(tts)
    key = center.post("音乐已暂停", status="success", source="voice", phase="done")
    with operation_scope(key):
        queue.enqueue(SpeechItem("音乐已暂停", PRIORITY_B, time.time() + 30))
    queue._running = True
    queue._worker_iteration()
    assert center.get(key).status == "success"
    assert center.get(key).speech_status == "disabled"
