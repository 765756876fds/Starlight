import sys
import types
import wave
from threading import Event

import pytest

from app.music.capture import CaptureCancelled, CaptureError, MicrophoneCapture


class _Stream:
    def __init__(self, fail=False):
        self.closed = False
        self.stopped = False
        self.fail = fail

    def read(self, count, exception_on_overflow=False):
        if self.fail:
            raise RuntimeError("device read failed")
        return b"\x00\x00" * count

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _PyAudio:
    last_stream = None
    fail_open = False
    fail_read = False
    terminated = False

    def open(self, **_kwargs):
        if self.fail_open:
            raise RuntimeError("no input device")
        self.last_stream = _Stream(self.fail_read)
        return self.last_stream

    def get_sample_size(self, _format):
        return 2

    def terminate(self):
        self.terminated = True


@pytest.fixture
def fake_pyaudio(monkeypatch):
    fake = _PyAudio()
    module = types.SimpleNamespace(PyAudio=lambda: fake, paInt16=8)
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    return fake


def test_capture_writes_expected_wav_and_releases(fake_pyaudio, tmp_path):
    result = MicrophoneCapture(seconds=1, sample_rate=8000, chunk=256).capture(str(tmp_path / "x.wav"))
    with wave.open(str(tmp_path / "x.wav"), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 8000
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 8000
    assert result["seconds"] == 1
    assert fake_pyaudio.last_stream.closed
    assert fake_pyaudio.last_stream.stopped
    assert fake_pyaudio.terminated


def test_capture_read_failure_releases(fake_pyaudio, tmp_path):
    fake_pyaudio.fail_read = True
    with pytest.raises(CaptureError, match="读取麦克风数据失败"):
        MicrophoneCapture(seconds=1).capture(str(tmp_path / "x.wav"))
    assert fake_pyaudio.last_stream.closed
    assert fake_pyaudio.terminated
    assert not (tmp_path / "x.wav").exists()


def test_capture_cancelled_releases_and_writes_nothing(fake_pyaudio, tmp_path):
    cancel = Event()
    cancel.set()
    with pytest.raises(CaptureCancelled):
        MicrophoneCapture(seconds=1).capture(str(tmp_path / "x.wav"), cancel)
    assert fake_pyaudio.last_stream.closed
    assert fake_pyaudio.terminated
    assert not (tmp_path / "x.wav").exists()


def test_capture_without_device_is_explicit(fake_pyaudio, tmp_path):
    fake_pyaudio.fail_open = True
    with pytest.raises(CaptureError, match="麦克风不可用"):
        MicrophoneCapture(seconds=1).capture(str(tmp_path / "x.wav"))
    assert fake_pyaudio.terminated


def test_capture_is_single_shot_and_has_no_listener_callback(fake_pyaudio, tmp_path):
    MicrophoneCapture(seconds=1).capture(str(tmp_path / "x.wav"))
    # 采集器只有一次 capture 调用，没有 start/监听线程或识曲回调入口。
    assert fake_pyaudio.last_stream.closed
