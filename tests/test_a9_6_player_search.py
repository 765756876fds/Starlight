"""A9.6-R3: 玩家搜索 Riot ID -> puuid 走 LCU /summoners/aliases。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lol.ongoing.player_search import (
    PlayerSearchService, parse_riot_id,
    FOUND, NOT_FOUND, INVALID_RIOT_ID, SEARCH_UNAVAILABLE,
)


class FakeLcu:
    """最小 fake：记录 post 调用，按脚本返回。"""

    def __init__(self, post_resp=None, post_exc=None):
        self.post_resp = post_resp
        self.post_exc = post_exc
        self.calls = []

    def get(self, ep):
        if ep == "/entitlements/v1/token":
            return {"accessToken": "FAKE"}
        return None

    def post(self, ep, body=None):
        self.calls.append((ep, body))
        if self.post_exc:
            raise self.post_exc
        return self.post_resp


def test_parse_ok():
    assert parse_riot_id("果子#48230") == ("果子", "48230")
    assert parse_riot_id(" 果子 # 48230 ") == ("果子", "48230")  # 空格被 strip
    assert parse_riot_id("abc") == (None, None)
    assert parse_riot_id("#1234") == (None, None)


def test_body_is_aliases_endpoint():
    lcu = FakeLcu(post_resp=[{"puuid": "PUU-1", "gameName": "果子", "tagLine": "48230"}])
    svc = PlayerSearchService(lcu.get, lcu.post)
    svc._token = lambda: "FAKE"
    # 战绩部分打桩，避免走真实 SGP
    import app.lol.ongoing.player_search as ps
    import app.lol.ongoing.sgp_provider as sgp_provider
    orig_resolver = ps.SgpServerResolver
    class _R:
        def __init__(self, *a): pass
        def resolve(self): return ("OK", "https://x", "GZ100")
    ps.SgpServerResolver = _R
    orig_provider = sgp_provider.SgpMatchHistoryProvider
    class _P:
        def __init__(self, *a): pass
        def recent(self, *a, **k):
            class R: records = []; ok = True
            return R()
    sgp_provider.SgpMatchHistoryProvider = _P
    try:
        r = svc.search("果子#48230")
    finally:
        ps.SgpServerResolver = orig_resolver
        sgp_provider.SgpMatchHistoryProvider = orig_provider
    ep, body = lcu.calls[0]
    assert ep == "/lol-summoner/v1/summoners/aliases"
    assert body == [{"gameName": "果子", "tagLine": "48230"}]
    assert r.puuid == "PUU-1"


def test_not_found_on_empty():
    lcu = FakeLcu(post_resp=[])
    svc = PlayerSearchService(lcu.get, lcu.post)
    r = svc.search("不存在#00001")
    assert r.status == NOT_FOUND


def test_unavailable_on_http_failure():
    lcu = FakeLcu(post_resp=None)  # LCU post 返回 None 表示非 2xx
    svc = PlayerSearchService(lcu.get, lcu.post)
    r = svc.search("果子#48230")
    assert r.status == SEARCH_UNAVAILABLE


def test_unavailable_on_exception():
    lcu = FakeLcu(post_exc=RuntimeError("boom"))
    svc = PlayerSearchService(lcu.get, lcu.post)
    r = svc.search("果子#48230")
    assert r.status == SEARCH_UNAVAILABLE


def test_invalid_input():
    lcu = FakeLcu()
    svc = PlayerSearchService(lcu.get, lcu.post)
    assert svc.search("abc").status == INVALID_RIOT_ID
    assert lcu.calls == []  # 不应发请求


def test_missing_puuid_is_unavailable():
    lcu = FakeLcu(post_resp=[{"gameName": "x", "tagLine": "1"}])
    svc = PlayerSearchService(lcu.get, lcu.post)
    r = svc.search("x#1")
    assert r.status == SEARCH_UNAVAILABLE


def test_no_old_endpoint():
    src = (Path(__file__).resolve().parents[1] / "app/lol/ongoing/player_search.py").read_text(encoding="utf-8")
    assert "/lol-summoner/v1/summoners?" not in src
    assert '"/lol-summoner/v1/summoners"' not in src


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL OK")
