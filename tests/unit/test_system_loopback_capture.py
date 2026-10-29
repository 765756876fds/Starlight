import sys
import types
from threading import Event

import numpy as np
import pytest

from app.music.capture import CaptureCancelled, CaptureError, SystemLoopbackCapture


class _Recorder:
    def __init__(self, fail=False):
        self.fail = fail
        self.closed = False
        self.calls = []

    def record(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("loopback unavailable")
        return np.full((kwargs["numframes"], kwargs["channels"]), 0.25, dtype=np.float32)

    def close(self):
        self.closed = True


def test_loopback_writes_pcm_and_closes(tmp_path):
    recorder = _Recorder()
    result = SystemLoopbackCapture(
        seconds=1, sample_rate=8000, channels=2, chunk_seconds=0.25,
        recorder_factory=lambda: recorder,
    ).capture(str(tmp_path / "system.wav"))
    assert result["sample_rate"] == 8000
    assert recorder.closed
    assert len(recorder.calls) == 4


def test_loopback_read_failure_closes_and_cleans(tmp_path):
    recorder = _Recorder(fail=True)
    with pytest.raises(CaptureError, match="读取 Windows 系统声音失败"):
        SystemLoopbackCapture(recorder_factory=lambda: recorder).capture(str(tmp_path / "x.wav"))
    assert recorder.closed
    assert not (tmp_path / "x.wav").exists()


def test_loopback_cancel_closes_and_writes_nothing(tmp_path):
    recorder = _Recorder()
    cancel = Event()
    cancel.set()
    with pytest.raises(CaptureCancelled):
        SystemLoopbackCapture(recorder_factory=lambda: recorder).capture(str(tmp_path / "x.wav"), cancel)
    assert recorder.closed
    assert not (tmp_path / "x.wav").exists()


def test_loopback_missing_soundcard_is_explicit(monkeypatch):
    monkeypatch.setitem(sys.modules, "soundcard", None)
    with pytest.raises(CaptureError, match="SoundCard"):
        SystemLoopbackCapture()._recorder()


def test_loopback_uses_soundcard_loopback_microphone_api(monkeypatch):
    speaker = types.SimpleNamespace(id="speaker-id")
    loopback = _Recorder()
    calls = []
    fake = types.SimpleNamespace(
        default_speaker=lambda: speaker,
        get_microphone=lambda device_id, include_loopback=False: (
            calls.append((device_id, include_loopback)) or loopback
        ),
    )
    monkeypatch.setitem(sys.modules, "soundcard", fake)
    assert SystemLoopbackCapture()._recorder() is loopback
    assert calls == [("speaker-id", True)]
