"""Online music 测试。"""
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
