"""JSMusicSource 单测（不真起 node）。"""
import json
import threading
import time

from app.music.online.sources.js_source import JSMusicSource, compare_versions
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


def test_parse_tracks_maps_cover_and_extra_fields():
    # P0 封面不显示：MusicFree 协议封面字段 image/artwork/cover 必须映射 cover_url；
    # extra 需补 id/platform/artwork（播放/下载链直接读）。
    s = JSMusicSource.__new__(JSMusicSource)
    s._sources = []
    raw = {
        "source_name": "酷我音乐",
        "track_id": "20505418",
        "title": "晴天", "artist": "周杰伦",
        "image": "http://x/cover.jpg",
    }
    out = s._parse_tracks([raw])
    assert len(out) == 1
    assert out[0].cover_url == "http://x/cover.jpg"
    assert out[0].extra.get("image") == "http://x/cover.jpg"

    raw2 = {"source_name": "kg", "track_id": "7", "title": "A",
            "id": "7", "platform": "kg", "artwork": "http://y/art.jpg"}
    out2 = s._parse_tracks([raw2])
    assert out2[0].cover_url == "http://y/art.jpg"
    assert out2[0].extra.get("id") == "7"
    assert out2[0].extra.get("platform") == "kg"

    # MusicFree 标准字段兜底：name/duration(秒)/cover（artists 数组归一在 bridge 侧）
    raw3 = {"source_name": "kg", "name": "B", "artist": "甲",
            "duration": 180, "cover": "http://z/c.jpg"}
    out3 = s._parse_tracks([raw3])
    assert out3[0].title == "B"
    assert out3[0].artist == "甲"
    assert out3[0].duration_seconds == 180
    assert out3[0].cover_url == "http://z/c.jpg"


def test_parse_tracks_keeps_raw_for_play():
    # 榜单/搜索播放依赖 extra._raw（bridge getUrl 用它构造 URL）
    s = JSMusicSource.__new__(JSMusicSource)
    s._sources = []
    raw = {"source_name": "酷我音乐", "track_id": "9", "title": "A",
           "extra": {"_raw": {"id": "9", "name": "A"}}}
    out = s._parse_tracks([raw])
    assert out[0].extra["_raw"]["id"] == "9"


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
    assert s.get_play_url(t) == {"url": "http://x/y.mp3", "headers": None}


def test_get_play_url_passes_plugin_headers(monkeypatch):
    """插件返回 headers（如开心汽水的 douyin Referer）必须透传到播放链。"""
    s = JSMusicSource.__new__(JSMusicSource)
    s._proc = FakeProc([])
    s._lock = threading.Lock()
    s._pending = {}
    monkeypatch.setattr(s, "_call", lambda m, p, timeout=15.0: {
        "url": "http://x/y.mp3",
        "headers": {"Referer": "https://www.douyin.com/", "Accept": "*/*"},
    })
    t = OnlineTrack(source_name="kg", track_id="1", title="x")
    assert s.get_play_url(t) == {
        "url": "http://x/y.mp3",
        "headers": {"Referer": "https://www.douyin.com/", "Accept": "*/*"},
    }


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


def test_list_plugins_returns_copies():
    """list_plugins 返回结构化插件列表的副本（改返回值不影响内部状态）。"""
    s = JSMusicSource.__new__(JSMusicSource)
    s._plugins = [{"name": "开心汽水", "type": "musicfree", "status": "ok"}]
    got = s.list_plugins()
    assert got == [{"name": "开心汽水", "type": "musicfree", "status": "ok"}]
    got[0]["name"] = "改坏了"
    assert s._plugins[0]["name"] == "开心汽水"


def test_search_plugin_targets_single_source(monkeypatch):
    """search_plugin 只搜指定插件（source 精确传递，不走 manager 遍历）。"""
    s = JSMusicSource.__new__(JSMusicSource)
    s._proc = FakeProc([])
    s._lock = threading.Lock()
    s._pending = {}
    calls = {}

    def fake_call(m, p, timeout=15.0):
        calls.update(p)
        return [{"source_name": "开心汽水", "track_id": "1", "title": "晴天"}]
    monkeypatch.setattr(s, "_call", fake_call)
    out = s.search_plugin("开心汽水", "晴天", 3)
    assert calls == {"source": "开心汽水", "keyword": "晴天", "limit": 3}
    assert out and out[0].source_name == "开心汽水" and out[0].title == "晴天"


def test_accepts_plugin_name_true():
    """accepts：内部插件名（"酷狗音乐"）命中；注册名（"js_bridge"）命中；未知返回 False。"""
    s = JSMusicSource.__new__(JSMusicSource)
    s._plugins = [
        {"name": "酷狗音乐", "type": "musicfree", "status": "ok"},
        {"name": "酷我音乐", "type": "musicfree", "status": "ok"},
        {"name": "lx (2)", "type": "lx", "status": "fail"},
    ]
    assert s.accepts(OnlineTrack(source_name="酷狗音乐", track_id="1", title="x")) is True
    assert s.accepts(OnlineTrack(source_name="lx (2)", track_id="1", title="x")) is True   # 含加载失败项（name 有值）
    assert s.accepts(OnlineTrack(source_name="js_bridge", track_id="1", title="x")) is True
    assert s.accepts(OnlineTrack(source_name="某外部源", track_id="1", title="x")) is False


def test_accepts_broken_plugins_falls_back_true():
    """_plugins 元素异常时放行（bridge 仍能按名路由）。"""
    s = JSMusicSource.__new__(JSMusicSource)
    s._plugins = ["不是dict"]
    assert s.accepts(OnlineTrack(source_name="酷狗音乐", track_id="1", title="x")) is True


def test_reload_wakes_pending_and_restarts(monkeypatch):
    """reload：清空 pending（主动唤醒等待方）+ 终止旧进程 + 重启等 _ready。"""
    s = JSMusicSource.__new__(JSMusicSource)
    s._lock = threading.Lock()
    box = {"event": threading.Event(), "result": "未决"}
    s._pending = {"rid1": box}

    class FakeProc:
        def __init__(self):
            self.killed = False
        def terminate(self):
            pass
        def wait(self, timeout):
            return 0
        def kill(self):
            self.killed = True
    s._proc = FakeProc()
    s._ready = threading.Event()

    def fake_start():
        s._proc = FakeProc()   # 新进程非 None
        s._ready.set()         # 模拟新 bridge 已就绪（reload 内部会先 clear 再重启）
    s._start_bridge = fake_start

    ok = s.reload()
    assert ok is True
    assert s._pending == {}          # pending 清空
    assert box["event"].is_set()     # 旧等待方被主动唤醒
    assert box["result"] is None     # 唤醒语义 = 失败（_call 返回 None）
    assert s._proc is not None       # 新进程已启动


# ---------- compare_versions（插件版本对比） ----------

def test_cmp_date_style_digits_compare_place_by_place():
    """日期型：2025.10.01 vs 2025.9.16 → 10 > 9（逐位整数比，非字符串比）。"""
    assert compare_versions("2025.10.01", "2025.9.16") == 1
    assert compare_versions("2025.9.16", "2025.10.01") == -1


def test_cmp_padding_short_versions():
    """长度不齐补 0：1 vs 1.0.0 相等；1.0 vs 1.0.1 小。"""
    assert compare_versions("1", "1.0.0") == 0
    assert compare_versions("1.0", "1.0.1") == -1
    assert compare_versions("2.0", "1.9.9") == 1


def test_cmp_strip_v_prefix():
    """剥 v/V 前缀：v1.2.3 == 1.2.3。"""
    assert compare_versions("v1.2.3", "1.2.3") == 0
    assert compare_versions("V2.0.0", "1.9.9") == 1


def test_cmp_single_number():
    """单数字：1.0.0 vs 1 → 相等；0.1.5 vs 1 → 小。"""
    assert compare_versions("1.0.0", "1") == 0
    assert compare_versions("0.1.5", "1") == -1
    assert compare_versions("1.0.0", "0.1.5") == 1


def test_cmp_parse_failure_returns_none():
    """解析失败返回 None（未知，不报有更新）；空值 None。"""
    assert compare_versions("abc", "1.0.0") is None
    assert compare_versions("1.0.0", "1.x.0") is None
    assert compare_versions("1.0.0", "") is None
    assert compare_versions(None, "1.0.0") is None
    assert compare_versions("", "") is None
