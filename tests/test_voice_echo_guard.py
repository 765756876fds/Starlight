"""
VOICE-ECHO-01: 回声抑制闸门测试（修1）

场景：
① TTS 正在播放（is_playing=True）→ _should_collect_speech 返回 False（丢弃本段）
② TTS 空闲（is_playing=False）→ 返回 True（正常采集）
③ 未注入 tts_provider（None）→ 返回 True（默认不启用，不改变旧行为）
④ tts_provider 抛异常 → 返回 True（TTS 异常不卡语音链）
"""
from unittest.mock import MagicMock

from app.voice.runtime import VoiceRuntime


def _make_runtime(tts_provider=None):
    return VoiceRuntime({"asr": {}, "intent": {}}, tts_provider=tts_provider)


def test_echo_dropped_when_tts_playing():
    tts = MagicMock()
    tts.is_playing.return_value = True
    runtime = _make_runtime(tts_provider=lambda: tts)
    assert runtime._should_collect_speech() is False


def test_collect_when_tts_idle():
    tts = MagicMock()
    tts.is_playing.return_value = False
    runtime = _make_runtime(tts_provider=lambda: tts)
    assert runtime._should_collect_speech() is True


def test_no_provider_defaults_true():
    runtime = _make_runtime(tts_provider=None)
    assert runtime._should_collect_speech() is True


def test_provider_exception_returns_true():
    def boom():
        raise RuntimeError("tts provider down")
    runtime = _make_runtime(tts_provider=boom)
    assert runtime._should_collect_speech() is True
