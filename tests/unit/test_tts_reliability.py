"""Bounded Edge synthesis and honest playback outcomes, with no real network/audio."""
import asyncio
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
import edge_tts

from app.speech.speech_queue import SpeechItem, SpeechQueue, PRIORITY_A, PRIORITY_B
from app.speech.tts import TTSController, TTSState

pytestmark = pytest.mark.unit


@pytest.fixture
def tts(tmp_path, monkeypatch):
    monkeypatch.setattr("app.speech.tts.get_event_bus", lambda: Mock())
    controller = TTSController({"engine": "edge", "output_dir": str(tmp_path),
                                "mpv_path": "unused-test-mpv"})
    controller._running = True
    monkeypatch.setattr(controller, "_resolve_mpv_path", lambda: True)
    process = Mock()
    process.wait.return_value = 0
    process.poll.return_value = 0
    monkeypatch.setattr("app.speech.tts.subprocess.Popen", lambda *a, **k: process)
    yield controller
    controller.stop()


def audio(tts):
    path = Path(tts.output_dir) / "generated.mp3"
    path.write_bytes(b"test-audio" * 300)
    return str(path)


def item(text, **kwargs):
    return SpeechItem(text, PRIORITY_B, time.time() + 30, **kwargs)


def events(tts, kind):
    return [call.args[0] for call in tts.event_bus.publish.call_args_list
            if call.args[0].event_type == kind]


def test_speak_reserves_busy_state_before_worker_scheduling(tts, monkeypatch):
    worker = Mock()
    monkeypatch.setattr("app.speech.tts.threading.Thread", lambda **k: worker)
    tts.speak("test")
    assert tts.get_state() == TTSState.GENERATING
    assert tts.is_playing() is True
    worker.start.assert_called_once()


def test_generation_exception_marks_failed_and_queue_processes_next(tts, monkeypatch):
    generated = audio(tts)
    generate = Mock(side_effect=[RuntimeError("synthetic generator failure"), generated])
    monkeypatch.setattr(tts, "_generate_audio", generate)
    queue = SpeechQueue(tts)
    queue._running = True
    first, second = item("failed"), item("next")
    queue.enqueue(first)
    queue.enqueue(second)
    queue._worker_iteration()
    assert first.status == "failed"
    assert tts.get_state() == TTSState.FAILED
    failure = tts.get_last_failure()
    assert failure[0] == tts._generation
    assert "语音播报失败" in failure[1]
    queue._worker_iteration()
    assert second.status == "finished"
    assert generate.call_count == 2
    assert len(events(tts, "speech.failed")) == 1
    assert len(events(tts, "speech.finished")) == 1
    assert tts.get_last_failure() == failure


@pytest.mark.parametrize("returncode", [1, -1])
def test_nonzero_mpv_exit_is_failure_not_completed(tts, monkeypatch, returncode):
    monkeypatch.setattr(tts, "_generate_audio", lambda text: audio(tts))
    process = Mock()
    process.wait.return_value = returncode
    process.poll.return_value = returncode
    monkeypatch.setattr("app.speech.tts.subprocess.Popen", lambda *a, **k: process)
    tts._speak_thread("test", tts._generation)
    assert tts.get_state() == TTSState.FAILED
    assert len(events(tts, "speech.failed")) == 1
    assert events(tts, "speech.finished") == []


@pytest.mark.parametrize("with_loop", [False, True])
def test_edge_timeout_is_bounded_and_cancels_save(tts, monkeypatch, with_loop):
    cancelled = threading.Event()
    async def save(path):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()
    communicate = Mock(save=save)
    monkeypatch.setattr("edge_tts.Communicate", lambda **k: communicate)
    tts.EDGE_TIMEOUT_SECONDS = 0.03
    loop = None
    if with_loop:
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        def run():
            loop.call_soon(ready.set)
            loop.run_forever()
        worker = threading.Thread(target=run)
        worker.start()
        assert ready.wait(1)
        tts._event_loop = loop
    try:
        start = time.monotonic()
        assert tts._generate_audio("non-private test") is None
        assert time.monotonic() - start < 1
        assert cancelled.wait(1)
        assert list(Path(tts.output_dir).glob("tts_*")) == []
    finally:
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            worker.join(1)
            tts._event_loop = None
            loop.close()


def test_edge_failure_log_keeps_exception_type_without_secret_url(tts, monkeypatch, caplog):
    async def save(path):
        raise TimeoutError()
    monkeypatch.setattr("edge_tts.Communicate", lambda **k: Mock(save=save))
    assert tts._generate_audio("test") is None
    assert "TimeoutError" in caplog.text


def test_event_loop_dispatch_timeout_cancels_the_submitted_future(tts, monkeypatch):
    future = Mock()
    future.result.side_effect = TimeoutError()
    def submit(coroutine, loop):
        coroutine.close()  # Simulate a loop that has not started this coroutine.
        return future
    loop = Mock()
    loop.is_running.return_value = True
    tts._event_loop = loop
    monkeypatch.setattr("app.speech.tts.asyncio.run_coroutine_threadsafe", submit)
    assert tts._generate_audio("test") is None
    future.cancel.assert_called_once()
    tts._event_loop = None


def test_edge_http500_is_not_retried_or_misreported_as_403(tts, monkeypatch, caplog):
    class ServerFailure(Exception):
        status = 500
    saves = []
    async def save(path):
        saves.append(path)
        raise ServerFailure("500, Invalid response status, token=private-test-value")
    monkeypatch.setattr("edge_tts.Communicate", lambda **k: Mock(save=save))
    monkeypatch.setattr("app.speech.tts.time.sleep", lambda seconds: None)
    assert tts._generate_audio("test") is None
    assert len(saves) == 1
    assert "status=500" in caplog.text
    assert "private-test-value" not in caplog.text


def test_playback_timeout_is_failed_and_termination_is_bounded(tts, monkeypatch):
    monkeypatch.setattr(tts, "_generate_audio", lambda text: audio(tts))
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("test-mpv", 60), None]
    monkeypatch.setattr("app.speech.tts.subprocess.Popen", lambda *a, **k: process)
    tts._speak_thread("test", tts._generation)
    process.terminate.assert_called_once()
    assert [call.kwargs["timeout"] for call in process.wait.call_args_list] == [60, 2]
    assert tts.get_state() == TTSState.FAILED
    assert events(tts, "speech.failed")[0].data["error"] == "playback_timeout"
    assert events(tts, "speech.finished") == []


def test_failed_synthesis_is_not_a_finished_queue_item(tts, monkeypatch):
    monkeypatch.setattr(tts, "_generate_audio", lambda text: None)
    queue = SpeechQueue(tts)
    queue._running = True
    failed = item("test")
    queue.enqueue(failed)
    queue._worker_iteration()
    assert failed.status == "failed"
    assert tts.get_state() == TTSState.FAILED


def test_inflight_voice_feedback_can_expire_without_cancelling_game_reminder():
    current = [True]
    tts = Mock(spec=["speak", "is_playing", "stop_playback"])
    def speak(text):
        current[0] = False  # New voice request arrives during synthesis/playback.
    tts.speak.side_effect = speak
    tts.is_playing.return_value = False
    queue = SpeechQueue(tts)
    queue._running = True
    old = item("old feedback", is_current=lambda: current[0])
    game = SpeechItem("normal game reminder", PRIORITY_A, time.time() + 30)
    queue.enqueue(old)
    queue._worker_iteration()
    assert old.status == "cancelled"
    queue.enqueue(game)
    queue._worker_iteration()
    assert game.status == "finished"
    assert [c.args[0] for c in tts.speak.call_args_list] == ["old feedback", "normal game reminder"]


def test_tts_failure_does_not_reexecute_successful_lol_command(tts, monkeypatch):
    from app.core.command_bus import CommandBus, CommandType as C
    from app.lol.cooldown.manager import CooldownManager
    from app.voice.runtime import VoiceRuntime
    manager = CooldownManager(scheduler=Mock())
    manager.set_game_time(1000)
    runtime = VoiceRuntime({"asr": {}, "intent": {}})
    runtime.asr = Mock()
    runtime.event_bus = Mock()
    runtime.command_bus = CommandBus()
    runtime.command_bus.register(C.COOLDOWN_START, manager.handle_start)
    queue = SpeechQueue(tts)
    queue._running = True
    runtime.speech_queue_provider = lambda: queue
    monkeypatch.setattr(tts, "_generate_audio", lambda text: None)
    try:
        for audio_bytes in (b"first-independent-command", b"second-independent-command"):
            runtime.asr.transcribe.return_value = "石头人闪现用了"
            runtime._process_speech(audio_bytes, 16000)
            queue._worker_iteration()
        assert len(manager.handle_list()) == 2
        assert len(runtime.command_bus.get_history()) == 2
        assert len(events(tts, "speech.failed")) == 2
    finally:
        manager.cancel_all()
