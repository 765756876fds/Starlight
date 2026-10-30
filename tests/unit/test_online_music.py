"""Online music 测试。"""
import pytest

pytestmark = pytest.mark.unit

import pytest

from app.music.online.models import OnlineTrack
from app.music.online.source import MusicSource
from app.music.online.manager import OnlineSourceManager
from app.music.online.cache import OnlineCache
from app.music.online.sources.demo_source import DemoSource


class SlowSource(MusicSource):
    name = "slow"
    display_name = "Slow"
    def search(self, keyword, limit=20):
        import time
        time.sleep(2)
        return [OnlineTrack(source_name="slow", track_id="s1", title="Slow Song")]
    def get_play_url(self, track): return "http://x"
    def get_lyric(self, track): return None


def test_source_abstract_not_instantiable():
    with pytest.raises(TypeError):
        MusicSource()


def test_register_unregister():
    m = OnlineSourceManager()
    s = DemoSource()
    m.register(s)
    assert "demo" in m.list_sources()
    m.unregister("demo")
    assert "demo" not in m.list_sources()


def test_demo_search():
    s = DemoSource()
    results = s.search("")
    assert len(results) == 2
    assert results[0].source_name == "demo"


def test_manager_search_all():
    m = OnlineSourceManager()
    m.register(DemoSource())
    out = m.search_all("")
    assert "demo" in out
    assert len(out["demo"]) == 2


def test_cache_hit():
    m = OnlineSourceManager()
    calls = {"n": 0}
    class CountingSource(DemoSource):
        def search(self, keyword, limit=20):
            calls["n"] += 1
            return super().search(keyword, limit)
    m.register(CountingSource())
    m.search_all("")
    m.search_all("")
    assert calls["n"] == 1  # 第二次命中缓存


def test_play_online_no_url_returns_false():
    """DemoSource 默认 extra.url 为空 → get_play_url 返回 None。"""
    m = OnlineSourceManager()
    m.register(DemoSource())
    track = m.search_all("")["demo"][0]
    assert m.get_play_url(track) is None


def test_cache_ttl():
    import time
    c = OnlineCache(ttl_seconds=0.05)
    c.set("s", "k", 5, [OnlineTrack(source_name="s", track_id="1", title="x")])
    assert c.get("s", "k", 5) is not None
    time.sleep(0.1)
    assert c.get("s", "k", 5) is None


def test_unregister_clears_cache():
    """注销音源时清理其全部缓存，避免同名替换后命中 stale 数据。"""
    m = OnlineSourceManager()
    m.register(DemoSource())
    m.search_all("")
    assert m._cache.get("demo", "", 10) is not None
    m.unregister("demo")
    assert m._cache.get("demo", "", 10) is None


def test_stale_search_result_discarded_after_source_replaced():
    """同名音源被替换后，在途旧搜索任务的结果必须被丢弃且不得写回缓存。"""
    import threading
    import time

    class SlowSource(MusicSource):
        name = "s"
        def search(self, keyword, limit=20):
            time.sleep(0.6)
            return [OnlineTrack(source_name="s", track_id="old", title="Old")]
        def get_play_url(self, track): return None
        def get_lyric(self, track): return None

    class FastSource(MusicSource):
        name = "s"
        def search(self, keyword, limit=20):
            return [OnlineTrack(source_name="s", track_id="new", title="New")]
        def get_play_url(self, track): return None
        def get_lyric(self, track): return None

    m = OnlineSourceManager()
    m.register(SlowSource())
    out = {}
    t = threading.Thread(target=lambda: out.update(m.search_all("x")))
    t.start()
    time.sleep(0.15)  # 慢源在途时替换同名源
    m.unregister("s")
    m.register(FastSource())
    t.join()
    # 旧 SlowSource 结果（0.6s 后完成）必须被版本校验丢弃
    assert "s" not in out
    assert m._cache.get("s", "x", 10) is None


def test_cache_hit_skips_request_after_replacement_invalidated():
    """替换同名源后旧缓存失效：下一次 search_all 会重新请求新源。"""
    import time

    class CountingSource(DemoSource):
        name = "c"
        def __init__(self, tag):
            super().__init__()
            self.tag = tag
            self.calls = 0
        def search(self, keyword, limit=20):
            self.calls += 1
            return [OnlineTrack(source_name="c", track_id=self.tag,
                                title=f"tag-{self.tag}")]

    m = OnlineSourceManager()
    a = CountingSource("A")
    m.register(a)
    m.search_all("x")
    assert a.calls == 1
    assert m._cache.get("c", "x", 10) is not None
    # 替换同名源（unregister 清缓存 + register 递增版本）
    m.unregister("c")
    b = CountingSource("B")
    m.register(b)
    out = m.search_all("x")
    assert b.calls == 1          # 重新请求而非命中旧缓存
    assert out["c"][0].track_id == "B"


def test_race_replacement_cannot_pollute_cache_after_fix():
    """确定性复现原竞争时序：旧任务完成→版本检查通过→cache.set 前一刻替换同名源。

    修复后（生命周期锁）：替换线程必须等 search_all 的"版本检查+缓存写入"
    临界区结束，随后 unregister 的缓存删除必然在旧结果写入之后执行 →
    旧结果 A 不可能残留在新源缓存中。"""
    import threading
    import time

    class SlowA(MusicSource):
        name = "s"
        def search(self, keyword, limit=20):
            time.sleep(0.4)
            return [OnlineTrack(source_name="s", track_id="A", title="A")]
        def get_play_url(self, track): return None
        def get_lyric(self, track): return None

    class FastB(MusicSource):
        name = "s"
        def __init__(self):
            self.calls = 0
        def search(self, keyword, limit=20):
            self.calls += 1
            return [OnlineTrack(source_name="s", track_id="B", title="B")]
        def get_play_url(self, track): return None
        def get_lyric(self, track): return None

    m = OnlineSourceManager()
    m.register(SlowA())
    orig_set = m._cache.set
    replaced = threading.Event()
    def do_replace():
        m.unregister("s")      # 等生命周期锁：必然在 search_all 临界区之后执行
        m.register(FastB())
        replaced.set()
    def hooked_set(*a, **k):
        # 旧任务结果写入缓存的"前一刻"，另一线程发起同名替换（原竞争时序）
        threading.Thread(target=do_replace, daemon=True).start()
        return orig_set(*a, **k)
    m._cache.set = hooked_set
    out = m.search_all("x")
    assert replaced.wait(2), "替换线程未执行"
    # 关键验收：旧结果 A 不得残留在新源缓存（unregister 的删除在 set 之后）
    assert m._cache.get("s", "x", 10) is None
    # 新源再次搜索：必须真实请求新源 B（而非命中旧缓存 A）
    m._cache.set = orig_set
    b = m._sources["s"]
    out2 = m.search_all("x")
    assert b.calls >= 1
    assert any(t.track_id == "B" for t in out2.get("s", []))
    assert not any(t.track_id == "A" for t in out2.get("s", []))
    m.close()


def test_get_play_url_falls_back_when_source_name_is_bridge_name():
    """track.source_name 为桥接内部音源名（如 js_bridge 返回的"酷我音乐"）
    而非注册名时，get_play_url 定向 miss 后应遍历已注册源取第一个可播放 URL，
    恢复 js_bridge（酷我）播放能力；定向命中路径（注册名匹配）不受影响。"""
    bridge_calls = []
    none_calls = []

    class BridgeLike(MusicSource):
        name = "js_bridge"
        display_name = "JS 音源桥接"
        def search(self, keyword, limit=20): return []
        def get_play_url(self, track):
            bridge_calls.append(track.source_name)
            return "https://bridge.example.com/a.mp3"
        def get_lyric(self, track): return None

    class NoUrl(MusicSource):
        name = "other"
        def search(self, keyword, limit=20): return []
        def get_play_url(self, track):
            none_calls.append(track.source_name)
            return None
        def get_lyric(self, track): return None

    m = OnlineSourceManager()
    m.register(NoUrl())          # 先注册：兜底遍历会先遇到它（返回 None，继续）
    m.register(BridgeLike())     # 后注册：第二个被调用，返回 URL

    t = OnlineTrack(source_name="酷我音乐", track_id="1", title="晴天")
    url = m.get_play_url(t)
    assert url == {"url": "https://bridge.example.com/a.mp3", "headers": None}
    assert none_calls == ["酷我音乐"]     # 兜底遍历按注册序，None 后继续
    assert bridge_calls == ["酷我音乐"]   # 桥接源收到原始 source_name（bridge 需要）

    # 定向命中路径不受影响：注册名 == track.source_name 时直接命中，不遍历
    m2 = OnlineSourceManager()
    direct_calls = []
    class Direct(MusicSource):
        name = "ikun"
        def search(self, keyword, limit=20): return []
        def get_play_url(self, track):
            direct_calls.append(track.source_name)
            return "https://ikun.example.com/b.mp3"
        def get_lyric(self, track): return None
    m2.register(Direct())
    t2 = OnlineTrack(source_name="ikun", track_id="2", title="歌")
    assert m2.get_play_url(t2) == {"url": "https://ikun.example.com/b.mp3", "headers": None}
    assert direct_calls == ["ikun"]
    assert m2._errors == {}


def test_get_play_url_accepts_preferred_on_miss():
    """定向 miss 时 accepts 命中的源优先（js_bridge 按插件名路由），
    不再先遍历其他源（避免对陌生 track 发无谓请求）。"""
    calls = []

    class AcceptingBridge(MusicSource):
        name = "js_bridge"
        def search(self, keyword, limit=20): return []
        def get_play_url(self, track):
            calls.append(("bridge", track.source_name))
            return "https://bridge.example.com/a.mp3"
        def get_lyric(self, track): return None
        def accepts(self, track):
            return track.source_name == "酷狗音乐"

    class Other(MusicSource):
        name = "other"
        def search(self, keyword, limit=20): return []
        def get_play_url(self, track):
            calls.append(("other", track.source_name))
            return None
        def get_lyric(self, track): return None

    m = OnlineSourceManager()
    m.register(Other())            # 先注册（旧全遍历会先碰它）
    m.register(AcceptingBridge())
    t = OnlineTrack(source_name="酷狗音乐", track_id="1", title="晴天")
    assert m.get_play_url(t) == {"url": "https://bridge.example.com/a.mp3", "headers": None}
    # other 不应被调用（accepts 命中轮直接到 bridge）
    assert calls == [("bridge", "酷狗音乐")]
    m.close()


def test_slow_source_timeout_does_not_hold_others():
    """慢源超时兜底：单个慢源被整体超时切断，不影响其他源结果。"""
    import time

    class SlowSource(MusicSource):
        name = "slow"
        def search(self, keyword, limit=20):
            time.sleep(3.0)
            return [OnlineTrack(source_name="slow", track_id="1", title="S")]
        def get_play_url(self, track): return None
        def get_lyric(self, track): return None

    class FastSource(MusicSource):
        name = "fast"
        def search(self, keyword, limit=20):
            return [OnlineTrack(source_name="fast", track_id="f1", title="F")]
        def get_play_url(self, track): return None
        def get_lyric(self, track): return None

    m = OnlineSourceManager(request_timeout=0.5)  # as_completed 上限 = 2.5s
    m.register(SlowSource())
    m.register(FastSource())
    t0 = time.time()
    out = m.search_all("x")
    elapsed = time.time() - t0
    assert elapsed < 2.6          # 不被 3s 慢源拖到 3s 以上
    assert out["fast"]            # 快源结果完整
    assert out["slow"] == []      # 慢源被超时切断 → 空
    assert m._errors.get("slow", "") != ""
    m.close()


def test_search_play_online_uses_direct_source_lookup(tmp_path):
    """回归：在线取播放 URL 必须定向到 chosen 源（manager.get_play_url 按
    track.source_name 路由），不能遍历所有源串行调用 get_play_url——
    旧逻辑会让 demo/archive 对陌生 track 各发一次 HTTP（超时 8s/个），
    导致在线播放最高 ~16s 延迟。"""
    from unittest.mock import MagicMock, patch
    from app.music.controller import MusicController
    from app.music.database import MusicDatabase

    db_path = str(tmp_path / "search_play.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    try:
        ctrl.mpv = MagicMock()
        ctrl.mpv.loadfile.return_value = {"error": "success"}  # loadfile 协议契约
        ctrl.set_movie_pauser(MagicMock(return_value=True))

        # 本地无结果 → 走在线 fallback
        ctrl.search = MagicMock()
        ctrl.search.search.return_value = []

        track = OnlineTrack(source_name="kw_bridge", track_id="t1",
                            title="稻香", artist="周杰伦")
        mgr = MagicMock()
        mgr.search_all.return_value = {"kw_bridge": [track]}
        mgr.get_play_url.return_value = {"url": "https://example.com/play", "headers": None}
        ctrl.online_manager = mgr

        result = ctrl.play_search_result("周杰伦稻香")
        assert result is not None
        # 只调 chosen 对应源取 URL，恰好一次
        mgr.get_play_url.assert_called_once_with(track)
        ctrl.mpv.loadfile.assert_called_once_with("https://example.com/play")
    finally:
        db.close()
