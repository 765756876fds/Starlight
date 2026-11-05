"""执行反馈与播报结果分开保存；无需麦克风或真实播放器。"""
from unittest.mock import Mock

import pytest

from app.core.command_bus import CommandBus, CommandType
from app.speech.speech_queue import SpeechQueue
from app.voice.intent import Intent
from app.voice.runtime import VoiceRuntime


def runtime():
    voice = VoiceRuntime({})
    voice.command_bus = CommandBus()
    voice.event_bus = Mock()
    return voice


def test_feedback_survives_unavailable_speech_backend():
    voice = runtime()
    voice._enqueue_speak("技能记录未成功")
    activity = voice.get_activity()
    assert activity["feedback"] == "技能记录未成功"
    assert activity["speech_status"] == "unavailable"


def test_result_is_visible_when_queue_rejects_speech():
    voice = runtime()
    voice.speech_queue_provider = lambda: Mock(enqueue=Mock(return_value=False))
    voice._enqueue_speak("倍速设置超时，暂时无法确认结果")
    activity = voice.get_activity()
    assert activity["speech_status"] == "rejected"
    assert "无法确认" in activity["feedback"]


@pytest.mark.parametrize("status", ["pending", "playing", "failed", "finished", "cancelled", "expired"])
def test_speech_status_does_not_change_the_business_result(status):
    voice = runtime()
    queue = SpeechQueue(Mock())
    voice.speech_queue_provider = lambda: queue
    voice._enqueue_speak("音乐暂停未完成")
    queue._pending[0][2].status = status
    activity = voice.get_activity()
    assert activity["feedback"] == "音乐暂停未完成"
    assert activity["speech_status"] == status


def test_new_command_clears_previous_feedback_without_claiming_success():
    voice = runtime()
    queue = SpeechQueue(Mock())
    voice.speech_queue_provider = lambda: queue
    voice._enqueue_speak("旧反馈")
    old = queue._pending[0][2]
    # 停止播报是无成功回调的真实已有入口。
    voice._stop_tts_now = Mock()
    voice._execute_intent(Intent("system.stop_tts", params={}), "别说了")
    old.status = "failed"
    activity = voice.get_activity()
    assert activity["heard"] == "别说了"
    assert activity["intent"] == "system.stop_tts"
    assert activity["feedback"] == ""
    assert activity["speech_status"] == "none"


def test_snapshot_is_detached_from_internal_state():
    voice = runtime()
    voice._enqueue_speak("当前反馈")
    snapshot = voice.get_activity()
    snapshot["feedback"] = "changed by caller"
    assert voice.get_activity()["feedback"] == "当前反馈"


def test_async_old_result_cannot_replace_the_latest_visible_feedback():
    voice = runtime()
    voice._voice_request_generation = 1
    old = voice._async_feedback(CommandType.MUSIC_SEARCH_PLAY, 1)
    voice._voice_request_generation = 2
    voice._enqueue_speak("新命令反馈")
    old(False, None)
    assert voice.get_activity()["feedback"] == "新命令反馈"
