"""设备故障只用内存替身，不访问真实麦克风。"""
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.voice.runtime import VoiceRuntime


@pytest.fixture
def voice():
    runtime = VoiceRuntime({})
    runtime._running = True
    runtime._mic_on = False  # start() 的状态初始化；不启动真实 ASR 服务。
    yield runtime
    runtime._running = False
    runtime._listening = False
    thread = runtime._mic_thread
    if isinstance(thread, threading.Thread):
        thread.join(timeout=2)


@pytest.mark.parametrize("stage", ["initialize", "open", "read"])
def test_microphone_failure_releases_resources_and_allows_retry(monkeypatch, stage, voice):
    stream = Mock()
    stream.read.side_effect = OSError("device unavailable")
    audio = Mock()
    audio.open.side_effect = OSError("device unavailable") if stage == "open" else None
    audio.open.return_value = stream
    constructor = Mock(side_effect=OSError("device unavailable")) if stage == "initialize" else Mock(return_value=audio)
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=constructor, paInt16=8))
    voice.toggle_mic()
    voice._mic_thread.join(timeout=1)
    assert not voice._mic_thread.is_alive(), "设备错误不能无限循环刷日志"
    assert not voice.is_mic_on()
    assert not voice.is_listening()
    assert "麦克风" in voice.get_activity()["feedback"]
    if stage != "initialize":
        audio.terminate.assert_called_once()
    if stage == "read":
        stream.close.assert_called_once()
    started = []
    monkeypatch.setattr(voice, "_start_microphone", lambda: started.append(True))
    assert voice.toggle_mic() is True
    assert started == [True]


def test_second_microphone_is_not_started_while_previous_is_releasing(monkeypatch, voice):
    voice._mic_on = False
    voice._mic_thread = SimpleNamespace(is_alive=lambda: True)
    start = Mock()
    monkeypatch.setattr(voice, "_start_microphone", start)
    assert voice.toggle_mic() is False
    start.assert_not_called()
    assert "释放" in voice.get_activity()["feedback"]


def test_turning_off_during_device_open_never_enters_capture_loop(monkeypatch, voice):
    opened, release = threading.Event(), threading.Event()
    stream, audio = Mock(), Mock()

    def slow_open(**kw):
        opened.set()
        assert release.wait(1)
        return stream

    audio.open.side_effect = slow_open
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=lambda: audio, paInt16=8))
    voice.toggle_mic()
    assert opened.wait(1)
    voice.toggle_mic()
    release.set()
    voice._mic_thread.join(timeout=1)
    assert not voice._mic_thread.is_alive()
    stream.read.assert_not_called()
    stream.close.assert_called_once()
    assert not voice.is_mic_on()


def test_empty_asr_text_has_visible_feedback_without_business_execution():
    voice = VoiceRuntime({})
    voice.asr = Mock(transcribe=Mock(return_value=""))
    voice._process_speech(b"sample", 16000)
    assert "没有识别到" in voice.get_activity()["feedback"]
