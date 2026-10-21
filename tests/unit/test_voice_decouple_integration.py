"""
VOICE-DECOUPLE-INT-01: 采集/识别解耦集成测试

核心场景 6 验证：ASR 慢响应（5s）期间，采集线程仍持续 stream.read
→ 识别期间说的话不再丢失。
"""
import pytest

pytestmark = pytest.mark.unit

import struct
import sys
import time
from unittest.mock import MagicMock

from app.voice.runtime import VoiceRuntime


def test_mic_reads_during_slow_asr():
    """ASR 慢响应（5s）期间，采集线程仍持续 stream.read（音频不丢）"""
    # 构造 mock pyaudio（采集线程内 import，注入 sys.modules）
    fake_pyaudio = MagicMock()
    fake_pyaudio.paInt16 = 8
    fake_stream = MagicMock()
    fake_audio = MagicMock()
    fake_audio.open.return_value = fake_stream
    fake_pyaudio.PyAudio.return_value = fake_audio

    loud = struct.pack("<1024h", *([800] * 1024))    # 音量 800 > 阈值 500
    silent = struct.pack("<1024h", *([0] * 1024))

    read_calls = {"n": 0}

    def _read(chunk, exception_on_overflow=False):
        read_calls["n"] += 1
        n = read_calls["n"]
        if n <= 8:
            return loud      # 前 8 帧语音（min_speech=5 → 8 帧 ✅）
        return silent        # 之后静音（max_silence=30 → 30 帧后段结束入队）

    fake_stream.read.side_effect = _read

    orig = sys.modules.get("pyaudio")
    sys.modules["pyaudio"] = fake_pyaudio
    try:
        rt = VoiceRuntime({"asr": {}, "intent": {}}, tts_provider=None)
        rt.asr = MagicMock()
        rt.asr.transcribe.side_effect = lambda *a, **k: (time.sleep(5) or "")
        rt.event_bus = MagicMock()
        rt.command_bus = MagicMock()

        rt.start()               # 建识别线程（asr.initialize 为 mock，无副作用）
        rt._start_microphone()   # 建采集线程（pyaudio 已 mock）

        # 等 VAD 段结束入队 + 识别线程取到段并挂起在 transcribe(5s) 上
        time.sleep(1.0)
        assert rt.asr.transcribe.call_count >= 1   # 识别已开始（挂起中）

        # 识别仍挂起期间：采集线程必须继续读麦克风 → 音频不丢
        before = read_calls["n"]
        time.sleep(1.5)
        assert read_calls["n"] > before            # 采集线程仍在 stream.read ✅

        rt.stop()
    finally:
        if orig is not None:
            sys.modules["pyaudio"] = orig
        else:
            sys.modules.pop("pyaudio", None)
