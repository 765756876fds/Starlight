import asyncio
import wave
from types import SimpleNamespace

from app.music.shazamio_provider import (
    ShazamIOProvider,
    ShazamRecognitionStatus,
)


def _wav(path, seconds=8, rate=16000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\0\0" * (seconds * rate))


def test_missing_audio_is_classified(tmp_path):
    result = ShazamIOProvider().recognize(str(tmp_path / "missing.wav"))
    assert result.status is ShazamRecognitionStatus.INVALID_AUDIO


def test_duration_is_validated_before_optional_import(tmp_path):
    path = tmp_path / "short.wav"
    _wav(path, seconds=2)
    result = ShazamIOProvider().recognize(str(path))
    assert result.status is ShazamRecognitionStatus.INVALID_AUDIO
    assert "8-12" in result.detail


def test_missing_optional_dependency_is_safe(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    _wav(path)
    real_import = __import__

    def blocked(name, *args, **kwargs):
        if name == "shazamio":
            raise ImportError("blocked in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)
    result = ShazamIOProvider().recognize(str(path))
    assert result.status is ShazamRecognitionStatus.DEPENDENCY_MISSING


def test_success_and_no_match_are_parsed_without_playback(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    _wav(path)

    class FakeClient:
        def __init__(self, payload):
            self.payload = payload
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def recognize(self, _path):
            return self.payload

    async def check(payload, expected):
        monkeypatch.setitem(__import__("sys").modules, "shazamio", SimpleNamespace(
            Shazam=lambda: FakeClient(payload), Serialize=object()))
        result = await ShazamIOProvider().recognize_async(str(path))
        assert result.status is expected
        return result

    result = asyncio.run(check({"track": {"title": "Song", "subtitle": "Artist", "key": "7"}},
                               ShazamRecognitionStatus.SUCCESS))
    assert (result.title, result.artist, result.track_id) == ("Song", "Artist", "7")
    asyncio.run(check({}, ShazamRecognitionStatus.NO_MATCH))


def test_timeout_is_classified_and_client_context_is_closed(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    _wav(path)
    closed = []

    class SlowClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            closed.append(True)
        async def recognize(self, _path):
            await asyncio.sleep(0.05)

    monkeypatch.setitem(__import__("sys").modules, "shazamio", SimpleNamespace(
        Shazam=SlowClient, Serialize=object()))
    result = asyncio.run(ShazamIOProvider(timeout_seconds=1.0 / 100).recognize_async(str(path)))
    assert result.status is ShazamRecognitionStatus.TIMEOUT
    assert closed
