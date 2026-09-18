"""JSMusicSource 单测（不真起 node）。"""
import json
import threading
import time

from app.music.online.sources.js_source import JSMusicSource
from app.music.online.models import OnlineTrack


class FakeProc:
    def __init__(self, responses):
        self.responses = responses
        self.stdin_lines = []
        self._stopped = False

    def write(self, s):
        self.stdin_lines.append(s)

    def flush(self): pass

    def terminate(self): self._stopped = True


def test_call_timeout_returns_none(monkeypatch):
    s = JSMusicSource.__new__(JSMusicSource)
    s._proc = FakeProc([])
    s._lock = threading.Lock()
    s._pending = {}
    s._sources = []
    # no reader thread → event never set
    out = s._call("ping", {}, timeout=0.2)
    assert out is None
    s.close()


def test_search_parses_response(monkeypatch):
    s = JSMusicSource.__new__(JSMusicSource)
    s._proc = FakeProc([])
    s._lock = threading.Lock()
    s._pending = {}
    s._sources = []
    # fake _call returning a list
    def fake_call(method, params, timeout=15.0):
        return [
            {"source_name": "kg", "track_id": "1", "title": "晴天",
             "artist": "周杰伦", "album": "叶惠美", "duration_seconds": 269, "extra": {}},
        ]
    monkeypatch.setattr(s, "_call", fake_call)
    out = s.search("周杰伦", limit=3)
    assert len(out) == 1
    assert out[0].title == "晴天"
    assert out[0].source_name == "kg"


def test_get_play_url_no_url(monkeypatch):
    s = JSMusicSource.__new__(JSMusicSource)
    s._proc = FakeProc([])
    s._lock = threading.Lock()
    s._pending = {}
    monkeypatch.setattr(s, "_call", lambda m, p, timeout=15.0: None)
    t = OnlineTrack(source_name="kg", track_id="1", title="x")
    assert s.get_play_url(t) is None


def test_get_play_url_returns_url(monkeypatch):
    s = JSMusicSource.__new__(JSMusicSource)
    s._proc = FakeProc([])
    s._lock = threading.Lock()
    s._pending = {}
    monkeypatch.setattr(s, "_call", lambda m, p, timeout=15.0: {"url": "http://x/y.mp3"})
    t = OnlineTrack(source_name="kg", track_id="1", title="x")
    assert s.get_play_url(t) == "http://x/y.mp3"


def test_lx_mock_provides_rawscript():
    import subprocess, sys
    r = subprocess.run(
        ["node", "-e",
         "const {createLxMock}=require('./app/music/online/js_bridge/lib/lx_mock.js');"
         "const lx=createLxMock();"
         "console.log(typeof lx.rawScript, typeof lx.utils.buffer, typeof lx.request, typeof lx.on);"],
        capture_output=True, text=True,
        cwd=r"C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent",
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip().split()
    assert out == ["function", "object", "function", "function"]
