import wave

from tools.verify_microphone_capture import _valid_wav


def test_valid_wav_requires_non_silent_pcm(tmp_path):
    path = tmp_path / "ok.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x01\x00" * 16_000)
    valid, message = _valid_wav(str(path))
    assert valid
    assert "WAV 有效" in message


def test_invalid_wav_rejects_silence(tmp_path):
    path = tmp_path / "silent.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 16_000)
    valid, message = _valid_wav(str(path))
    assert not valid
    assert "静音" in message
