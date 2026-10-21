"""TraceRecorder + RecordingMpv 基建测试：开关行为、透明代理、异常穿透、脱敏、轮转。"""
import json

import pytest

from app.music.recording_mpv import RecordingMpv
from app.music.trace_recorder import TraceRecorder


class _FakeMpv:
    """极简替身：模拟 MpvAdapter 协议（不发真声音）。"""

    def __init__(self):
        self.loadfile_calls = []
        self._props = {"playback-time": 12.5, "idle-active": False}
        self._paused = False

    def get_property(self, name, timeout=2.0):
        return self._props.get(name)

    def loadfile(self, filepath, mode="replace", **kwargs):
        self.loadfile_calls.append((filepath, mode))
        return "loaded"

    def pause(self):
        self._paused = True

    def play(self):
        self._paused = False

    def toggle_pause(self):
        self._paused = not self._paused

    def stop(self):
        self._paused = True

    def is_idle(self):
        return False

    def get_filename(self):
        return "song.flac"

    def boom(self):
        raise RuntimeError("boom")


@pytest.fixture
def recorder(tmp_path):
    r = TraceRecorder(output_dir=str(tmp_path), enabled=True)
    yield r
    r.flush()


def _entries(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---- 开关行为 ----

def test_disabled_recorder_ignores_records(tmp_path):
    r = TraceRecorder(output_dir=str(tmp_path), enabled=False)
    r.record("mpv", {"m": "pause"})
    r.flush()
    assert r.pending == 0
    assert r.dropped == 0
    # 关闭时不应产生任何文件
    assert not list(tmp_path.glob("trace_*.jsonl"))


def test_record_flush_writes_jsonl_with_header(recorder, tmp_path):
    recorder.record("mpv", {"m": "pause", "dur_ms": 0.1})
    recorder.record("mpv", {"m": "get_property", "args": {"args": ["playback-time"],
                                                           "kwargs": {}}, "res": 12.5})
    recorder.flush()
    path = tmp_path / "trace_0.jsonl"
    assert path.exists()
    rows = _entries(path)
    # header + 2 条记录
    assert len(rows) == 3
    header = rows[0]
    assert header["v"] == "1.0"
    assert header["pid"] == __import__("os").getpid()
    assert header["clock"] == "perf_counter"
    assert "start_wall" in header
    # 记录行字段规范
    e1, e2 = rows[1], rows[2]
    assert e1["kind"] == "mpv" and e2["kind"] == "mpv"
    assert e1["m"] == "pause" and e2["m"] == "get_property"
    assert e1["seq"] < e2["seq"], "seq 全局自增"
    assert "t" in e1 and "th" in e1 and "dur_ms" in e1


# ---- RecordingMpv 透明代理 ----

def test_transparent_proxy(recorder):
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)
    # 返回值透传
    assert wrapped.get_property("playback-time") == 12.5
    assert wrapped.is_idle() is False
    assert wrapped.get_filename() == "song.flac"
    # loadfile 正常调用并透传返回值
    assert wrapped.loadfile("C:/Music/a.flac") == "loaded"
    assert fake.loadfile_calls == [("C:/Music/a.flac", "replace")]
    # 未拦截属性直接透传
    assert wrapped.loadfile_calls is fake.loadfile_calls


def test_exception_passthrough(recorder, tmp_path):
    """包装方法抛异常：原样 re-raise（穿透），且 exc 字段被记录，不影响后续调用。"""
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)

    def _boom_get(name, timeout=2.0):
        raise RuntimeError("boom")

    fake.get_property = _boom_get
    with pytest.raises(RuntimeError, match="boom"):
        wrapped.get_property("playback-time")
    # 后续正常调用不受影响
    assert wrapped.is_idle() is False
    recorder.flush()
    rows = _entries(tmp_path / "trace_0.jsonl")
    exc_rows = [r for r in rows if r.get("exc")]
    assert any("RuntimeError: boom" in r["exc"] for r in exc_rows), \
        f"异常应被记录，实际: {exc_rows}"


def test_wrapped_methods_recorded(recorder):
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)
    wrapped.pause()
    wrapped.play()
    wrapped.toggle_pause()
    wrapped.stop()
    recorder.flush()
    rows = _entries(recorder._current_path())
    ms = [r["m"] for r in rows if r.get("kind") == "mpv"]
    assert ms == ["pause", "play", "toggle_pause", "stop"]
    for r in rows[1:]:
        assert r["th"]  # 线程名非空


# ---- 脱敏 ----

def test_sanitize_local_path_no_dir(recorder, tmp_path):
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)
    wrapped.loadfile("C:/secret/dir/song.flac")
    recorder.flush()
    rows = _entries(tmp_path / "trace_0.jsonl")
    load = [r for r in rows if r.get("m") == "loadfile"][0]
    args = load["args"]["args"]
    assert "song.flac" in args[0]
    assert "secret" not in args[0], "本地路径不得包含目录"
    assert load["file_kind"] == "local"


def test_sanitize_online_url_no_secret(recorder, tmp_path):
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)
    url = "https://example.com/api/song?id=1&token=abc123"
    wrapped.loadfile(url, headers={"Authorization": "Bearer s3cr3t",
                                   "Referer": "https://ref.example.com"})
    recorder.flush()
    rows = _entries(tmp_path / "trace_0.jsonl")
    load = [r for r in rows if r.get("m") == "loadfile"][0]
    args = load["args"]["args"]
    assert "token" not in args[0], "在线 URL 不得含签名参数"
    assert "example.com" in args[0]
    assert "..." in args[0] and len(args[0].split("...")[-1]) == 8, "应含短 hash"
    assert load["file_kind"] == "online"
    # headers 只记 key，不记 value
    h = load["args"]["kwargs"]["headers"]
    assert h == {"Authorization": "<redacted>", "Referer": "<redacted>"}
    assert "s3cr3t" not in json.dumps(load, ensure_ascii=False)


# ---- 轮转 ----

def test_rotation_keeps_three_files(tmp_path):
    r = TraceRecorder(output_dir=str(tmp_path), enabled=True, max_bytes=100, keep=3)
    for i in range(40):
        r.record("mpv", {"m": "pause", "i": i, "pad": "x" * 80})
        r.flush()  # 模拟持续写入：每条落盘后立即触发轮转判定
    files = sorted(p.name for p in tmp_path.glob("trace_*.jsonl"))
    assert files == ["trace_0.jsonl", "trace_1.jsonl", "trace_2.jsonl"], \
        "轮转后只保留 3 份"
    # 每份文件首行都是 header（可独立解析）
    for name in files:
        rows = _entries(tmp_path / name)
        assert rows[0]["v"] == "1.0"
        assert len(rows) >= 2, f"{name} 应含 header + 至少 1 条记录"


def test_get_trace_recorder_singleton():
    from app.music.trace_recorder import get_trace_recorder
    a = get_trace_recorder(enabled=False)
    b = get_trace_recorder(enabled=False)
    assert a is b
