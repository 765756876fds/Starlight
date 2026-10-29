from types import SimpleNamespace

import pytest

import app.music.humming as humming


def _service(monkeypatch):
    service = humming.HummingService.__new__(humming.HummingService)
    service.record_seconds = 8
    calls = []

    class FakeCapture:
        def __init__(self, **kwargs):
            calls.append(("mic_init", kwargs))

        def capture(self, path):
            calls.append(("mic_capture", path))

    class FakeLoopback:
        def __init__(self, **kwargs):
            calls.append(("system_init", kwargs))

        def capture(self, path):
            calls.append(("system_capture", path))

    monkeypatch.setattr(humming, "MicrophoneCapture", FakeCapture)
    monkeypatch.setattr(humming, "SystemLoopbackCapture", FakeLoopback)
    monkeypatch.setattr(service, "identify_file", lambda path: {
        "title": "晴天", "filepath": path, "confidence": 0.9,
    })
    return service, calls


@pytest.mark.parametrize("source, expected", [
    ("microphone", "mic_capture"),
    ("system", "system_capture"),
])
def test_record_and_identify_routes_to_selected_capture(monkeypatch, tmp_path, source, expected):
    service, calls = _service(monkeypatch)
    result = service.record_and_identify(str(tmp_path / "clip.wav"), source=source)
    assert result["source"] == source
    assert any(item[0] == expected for item in calls)
    assert not any(item[0] == ("system_capture" if source == "microphone" else "mic_capture") for item in calls)


def test_record_and_identify_rejects_unknown_source(monkeypatch, tmp_path):
    service, calls = _service(monkeypatch)
    with pytest.raises(humming.HummingError, match="不支持的识曲输入来源"):
        service.record_and_identify(str(tmp_path / "clip.wav"), source="unknown")
    assert calls == []
