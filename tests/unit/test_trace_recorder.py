"""TraceRecorder + RecordingMpv 基建测试：开关行为、透明代理、异常穿透、脱敏、轮转/清理。"""
import json
import os

import pytest

from app.music.recording_mpv import RecordingMpv
from app.music.trace_recorder import TraceRecorder


class _FakeMpv:
    """极简替身：模拟 MpvAdapter 协议（不发真声音）。"""

    def __init__(self):
        self.loadfile_calls = []
        self._props = {"playback-time": 12.5, "idle-active": False, "pause": False}
        self._paused = False

    def get_property(self, name, timeout=2.0):
        return self._props.get(name)

    def set_property(self, name, value):
        self._props[name] = value

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

    def start(self):
        return "started"

    def stop_process(self):
        return None

    def is_running(self):
        return True

    def is_idle(self):
        return False

    def get_filename(self):
        return "song.flac"


@pytest.fixture
def recorder(tmp_path):
    r = TraceRecorder(output_dir=str(tmp_path), enabled=True)
    yield r
    r.flush()


def _trace_files(dirpath):
    return sorted(str(p) for p in dirpath.glob("trace_*.jsonl"))


def _entries(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _only_file(recorder, tmp_path) -> str:
    files = _trace_files(tmp_path)
    assert len(files) == 1, f"期望单文件，实际: {files}"
    return files[0]


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
    path = _only_file(recorder, tmp_path)
    assert os.path.basename(path).startswith("trace_"), "命名须为 trace_<start_wall>_<pid>_<n>.jsonl"
    rows = _entries(path)
    # header + 2 条记录
    assert len(rows) == 3
    header = rows[0]
    assert header["v"] == "1.0"
    assert header["pid"] == os.getpid()
    assert header["clock"] == "perf_counter"
    assert "start_wall" in header and "start_perf" in header, "header 须含 start_wall 与 start_perf"
    assert header["app_commit"], "app_commit 须读真实 git hash（或 unknown），不能为空"
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
    """包装方法抛异常：原样 re-raise（穿透），exc 字段被记录（截断到 100 字符），
    且不影响后续调用。"""
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)

    def _boom_get(name, timeout=2.0):
        raise RuntimeError("x" * 300)  # 超长异常 → 记录文本必须截断

    fake.get_property = _boom_get
    with pytest.raises(RuntimeError, match="xxx"):
        wrapped.get_property("playback-time")
    # 后续正常调用不受影响
    assert wrapped.is_idle() is False
    recorder.flush()
    rows = _entries(_only_file(recorder, tmp_path))
    exc_rows = [r for r in rows if r.get("exc")]
    assert exc_rows, "异常应被记录"
    text = exc_rows[0]["exc"]
    assert text.startswith("RuntimeError:")
    assert len(text) <= 103, f"exc 文本须截断到 100 字符，实际长度 {len(text)}: {text!r}"


def test_wrapped_methods_recorded(recorder, tmp_path):
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)
    wrapped.pause()
    wrapped.play()
    wrapped.toggle_pause()
    wrapped.stop()
    recorder.flush()
    rows = _entries(_only_file(recorder, tmp_path))
    ms = [r["m"] for r in rows if r.get("kind") == "mpv"]
    assert ms == ["pause", "play", "toggle_pause", "stop"]
    for r in rows[1:]:
        assert r["th"]  # 线程名非空


def test_set_property_whitelist_and_self_heal(recorder, tmp_path):
    """set_property 白名单只记 pause 的值；start/stop_process/is_running 补记录。"""
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)
    wrapped.set_property("pause", True)      # 白名单：记真实值
    wrapped.set_property("volume", 50)       # 非白名单：值脱敏
    wrapped.start()
    wrapped.stop_process()
    assert wrapped.is_running() is True
    recorder.flush()
    rows = _entries(_only_file(recorder, tmp_path))
    by_m = {}
    for r in rows:
        if r.get("kind") == "mpv":
            by_m.setdefault(r["m"], []).append(r)
    assert "set_property" in by_m
    set_rows = {r["args"]["args"][0]: r["args"]["args"][1] for r in by_m["set_property"]}
    assert set_rows.get("pause") is True, "pause 白名单须记真实值"
    assert set_rows.get("volume") == "<redacted>", "非白名单属性只记 key 不记 value"
    assert "start" in by_m and "stop_process" in by_m and "is_running" in by_m


# ---- 脱敏 ----

def test_sanitize_local_path_no_dir(recorder, tmp_path):
    fake = _FakeMpv()
    wrapped = RecordingMpv(fake, recorder)
    wrapped.loadfile("C:/secret/dir/song.flac")
    recorder.flush()
    rows = _entries(_only_file(recorder, tmp_path))
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
    rows = _entries(_only_file(recorder, tmp_path))
    load = [r for r in rows if r.get("m") == "loadfile"][0]
    args = load["args"]["args"]
    # 只留 host + "..." + 8 位 hash，不带 URL 路径/参数
    assert "api/song" not in args[0] and "token" not in args[0]
    assert "example.com" in args[0] and "..." in args[0]
    assert len(args[0].split("...")[-1]) == 8, "应含 8 位短 hash"
    assert load["file_kind"] == "online"
    # headers 只记 key，不记 value
    h = load["args"]["kwargs"]["headers"]
    assert h == {"Authorization": "<redacted>", "Referer": "<redacted>"}
    assert "s3cr3t" not in json.dumps(load, ensure_ascii=False)


# ---- 轮转与清理 ----

def test_rotation_new_files_and_prune_to_10(tmp_path):
    """满 max_bytes 轮转产生新文件（n 递增，不覆盖旧文件）；运行中收敛保留 ≤10。"""
    r = TraceRecorder(output_dir=str(tmp_path), enabled=True, max_bytes=100)
    for i in range(40):
        r.record("mpv", {"m": "pause", "i": i, "pad": "x" * 80})
        r.flush()  # 每条落盘即触发轮转判定
    files = _trace_files(tmp_path)
    assert 1 < len(files) <= 10, f"轮转应产生多文件且收敛保留 ≤10，实际 {len(files)}"
    # 命名格式：trace_<start_wall>_<pid>_<n>.jsonl
    for f in files:
        base = os.path.basename(f)
        assert base.startswith(f"trace_{int(r._start_wall)}_{os.getpid()}_"), base
        rows = _entries(f)
        assert rows[0]["v"] == "1.0", f"{base} 首行应为 header"


def test_startup_cleanup_keeps_recent_10(tmp_path):
    """启动时按 mtime 保留最近 10 个 trace 文件，清理更旧。"""
    old = []
    for i in range(15):
        p = tmp_path / f"trace_1000_1_{i}.jsonl"
        p.write_text('{"v":"1.0","app_commit":"old","pid":1,"clock":"perf_counter",'
                     '"start_wall":0,"start_perf":0}\n', encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))  # 按 mtime 递增
    r = TraceRecorder(output_dir=str(tmp_path), enabled=True)
    r.flush()
    # 15 旧文件 → 保留 10 个最新的（本运行新文件 mtime 最新，也在保留列）
    files = sorted(os.path.getmtime(p) for p in tmp_path.glob("trace_*.jsonl"))
    assert len(files) == 10, f"启动清理后应保留最近 10 个，实际 {len(files)}"
    assert files == sorted(files)


# ---- 坏记录容错 ----

def test_bad_record_dropped_not_kill_batch(recorder, tmp_path):
    """循环引用/不可序列化记录：只计入 dropped，其余记录正常落盘。"""
    bad = {}
    bad["self"] = bad  # 循环引用 → json.dumps 抛 ValueError
    recorder.record("mpv", {"m": "good_1"})
    recorder.record("mpv", {"m": "bad", "data": bad})
    recorder.record("mpv", {"m": "good_2"})
    recorder.flush()
    assert recorder.dropped == 1, "坏记录应计入 dropped"
    rows = _entries(_only_file(recorder, tmp_path))
    ms = [r["m"] for r in rows if r.get("kind") == "mpv"]
    assert ms == ["good_1", "good_2"], "好记录不受坏记录影响"


def test_get_trace_recorder_singleton():
    from app.music.trace_recorder import get_trace_recorder
    a = get_trace_recorder(enabled=False)
    b = get_trace_recorder(enabled=False)
    assert a is b
