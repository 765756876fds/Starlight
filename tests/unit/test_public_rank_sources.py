from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from app.movie import discovery
from app.movie.discovery import CatalogReadError, MovieDiscovery, PLATFORMS
from app.movie.rank_sources import IqiyiRank, TencentRank, image_url


def iqiyi_page(channel="1", title="示例作品", rank="1", url="//www.iqiyi.com/v_abc.html"):
    return f'''<a class="rtab__slider__link selected" aria-current="page" data-rseat-v2="channel.{channel}">电影</a>
<span class="erji__meta__txt">按实时热度排行最近更新11-05 14:55</span>
<a class="rvi__box rvi__box3" data-rseat-v2="{rank}" href="{url}">
<picture><img class="i71-img" src="//pic1.iqiyipic.com/example.jpg"></picture>
<div class="rvi__tit1" title="{title}"><span>1</span>{title}</div></a>
<a href="//www.iqiyi.com/v_recommend.html" title="推荐，不是榜单">相关推荐</a>
<script>window.__NUXT__=execute_arbitrary_code();</script>'''


def tencent_payload(channel="1", items=None):
    if items is None:
        items = [dict(Query="示例电影", ID="abc", URL="https://not-official.invalid/video", VideoType=channel,
                      HzImgURL="https://vcover-hz-pic.puui.qpic.cn/example.jpg")]
    return dict(ret=0, data=dict(ErrCode=0, Results=[dict(ChannelID=channel, ChannelName="电影", Result=dict(ListInfo=items))]))


@pytest.mark.parametrize("category,channel", IqiyiRank.CHANNELS.items())
def test_iqiyi_reads_only_selected_category_rank_cards_and_never_evaluates_web_scripts(category, channel):
    calls = []
    provider = IqiyiRank(fetch=lambda url: calls.append(url) or iqiyi_page(channel))
    row, = provider.read(category)
    assert calls == [f"https://www.iqiyi.com/ranks1/{channel}/0"]
    assert row["kind"] == category and row["platform"] == "iqiyi" and row["rank"] == 1
    assert row["title"] == "示例作品" and row["url"] == "https://www.iqiyi.com/v_abc.html"
    assert row["source_updated"].endswith("11-05 14:55") and row["board"] == "热度榜"
    assert row["year"] is None and row["region"] is None and row["season"] is None
    assert row["cover_url"] == "https://pic1.iqiyipic.com/example.jpg" and "play_url" not in row


@pytest.mark.parametrize("page", ["captcha", "<html>loading</html>", iqiyi_page(channel="2"),
                                  iqiyi_page(rank="0"), iqiyi_page(url="https://iqiyi.com.evil.invalid/v_a"),
                                  iqiyi_page(title=""), iqiyi_page(rank="many")])
def test_iqiyi_blocked_or_changed_page_is_a_failure_not_an_empty_success(page):
    with pytest.raises(CatalogReadError):
        IqiyiRank(fetch=lambda _: page).read("movie")


def test_iqiyi_duplicate_cards_do_not_duplicate_the_same_official_link():
    page = iqiyi_page() * 2
    assert len(IqiyiRank(fetch=lambda _: page).read("movie")) == 1


@pytest.mark.parametrize("category,channel", TencentRank.CHANNELS.items())
def test_tencent_preserves_hot_search_provenance_and_uses_official_search_not_an_invented_player(category, channel):
    calls = []
    row, = TencentRank(fetch=lambda u: calls.append(u) or tencent_payload(channel)).read(category)
    assert parse_qs(urlsplit(calls[0]).query) == dict(ChannelID=[channel], PageSize=["50"], PageNum=["0"], SceneID=["2"])
    assert row["kind"] == category and row["platform"] == "tencent" and row["board"] == "热搜榜"
    assert row["url_kind"] == "search" and row["url"].startswith("https://v.qq.com/x/search/?")
    assert parse_qs(urlsplit(row["url"]).query)["q"] == ["示例电影"]
    assert row["year"] is None and "play_url" not in row


@pytest.mark.parametrize("payload", [None, {}, dict(ret=1), dict(ret=False), dict(ret=0, data={}),
                                      tencent_payload("2"), tencent_payload(items=[{}])])
def test_tencent_rejection_or_missing_channel_cannot_become_empty_success(payload):
    with pytest.raises(CatalogReadError):
        TencentRank(fetch=lambda _: payload).read("movie")


def test_verified_empty_tencent_list_is_cacheable_and_failure_is_immediately_retryable(tmp_path):
    calls = []
    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise requests.Timeout()
        return tencent_payload(items=[])
    service = MovieDiscovery(tmp_path / "ranks.json", [TencentRank(fetch)])
    assert service.read("movie")["states"][0]["state"] == "failed"
    assert service.read("movie")["states"][0]["state"] == "empty"
    assert service.read("movie")["states"][0]["state"] == "cached"
    assert len(calls) == 2


@pytest.mark.parametrize("url,domain,expected", [("//pic2.iqiyipic.com/a", "iqiyipic.com", "https://pic2.iqiyipic.com/a"),
    ("https://qpic.cn.evil.invalid/a", "qpic.cn", ""), ("http://qpic.cn/a", "qpic.cn", "https://qpic.cn/a"),
    ("http://qpic.cn.evil.invalid/a", "qpic.cn", ""),
    ("file:///private/a", "iqiyipic.com", ""), (None, "qpic.cn", "")])
def test_image_allowlist_rejects_other_origins_and_local_files(url, domain, expected):
    assert image_url(url, domain) == expected
    if expected:
        assert image_url(url, domain).startswith("https://")


def test_default_platforms_distinguish_real_feeds_and_official_links(tmp_path):
    service = MovieDiscovery(tmp_path / "ranks.json")
    assert {p.key for p in service.providers} == {"bilibili", "iqiyi", "tencent"}
    assert {p.key for p in PLATFORMS if not p.automatic} == {"youku", "mango"}


def test_default_sources_keep_partial_success_and_their_own_ranks(tmp_path, monkeypatch):
    def fetch(url, **kwargs):
        if "api.bilibili.com" in url:
            raise requests.Timeout()
        return iqiyi_page() if "iqiyi" in url else tencent_payload()
    monkeypatch.setattr(discovery, "fetch_public", fetch)
    result = MovieDiscovery(tmp_path / "ranks.json").read("movie")
    assert len(result["works"]) == 2, "Unknown release metadata cannot be forcibly merged"
    assert {row["platform"] for row in result["works"]} == {"iqiyi", "tencent"}
    assert {s["state"] for s in result["states"]} == {"failed", "success"}
    assert result["totals"]["movie"] == 2
    assert {s["board"] for row in result["works"] for s in row["sources"]} == {"热度榜", "热搜榜"}


def test_public_transport_is_bounded_and_does_not_supply_credentials(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size):
            yield b'{"ok":true}'
    monkeypatch.setattr(discovery.requests, "get", lambda *args, **kwargs: calls.append((args, kwargs)) or Response())
    assert discovery.fetch_public("https://example.invalid") == {"ok": True}
    kwargs = calls[0][1]
    assert kwargs["timeout"] == (3, 6) and kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert not {"cookies", "auth", "proxies", "verify"}.intersection(kwargs)


def test_public_transport_rejects_oversized_responses(monkeypatch):
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size):
            yield b" " * 2_000_001
    monkeypatch.setattr(discovery.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(CatalogReadError):
        discovery.fetch_public("https://example.invalid")
