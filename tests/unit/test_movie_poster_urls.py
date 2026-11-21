"""Trusted public CDN URLs retain the original image identity over HTTPS."""
import pytest

from app.movie.discovery import BilibiliRank
from app.movie.rank_sources import image_url


@pytest.mark.parametrize("domain,path", [("hdslb.com", "i0.hdslb.com/bfs/bangumi/same.jpg"),
                                         ("iqiyipic.com", "pic1.iqiyipic.com/image/same.jpg"),
                                         ("qpic.cn", "puui.qpic.cn/vcover_hz_pic/0/same/0")])
@pytest.mark.parametrize("prefix", ["http://", "https://", "//"])
def test_public_poster_http_urls_are_upgraded_without_changing_the_image(domain, path, prefix):
    read = BilibiliRank.cover_url if domain == "hdslb.com" else lambda value: image_url(value, domain)
    assert read(prefix + path) == "https://" + path


@pytest.mark.parametrize("value", ["http://hdslb.com.evil.invalid/same.jpg", "file:///poster.jpg",
                                  "data:image/png;base64,invalid", None, ""])
def test_untrusted_or_non_network_poster_urls_remain_rejected(value):
    assert BilibiliRank.cover_url(value) == ""
    assert image_url(value, "qpic.cn") == ""


@pytest.mark.parametrize("domain", ["hdslb.com", "iqiyipic.com"])
@pytest.mark.parametrize("address", ["https://[broken/poster.jpg", "https://[::1/poster.jpg",
                                     "https://pic.{domain}:wrong/poster.jpg",
                                     "https://pic.{domain}:99999/poster.jpg"])
def test_malformed_poster_address_becomes_a_missing_poster(domain, address):
    read = BilibiliRank.cover_url if domain == "hdslb.com" else lambda v: image_url(v, domain)
    assert read(address.format(domain=domain)) == ""


@pytest.mark.parametrize("domain", ["hdslb.com", "iqiyipic.com"])
@pytest.mark.parametrize("prefix", ["http://", "https://", "//"])
def test_poster_address_outer_whitespace_is_normalized(domain, prefix):
    read = BilibiliRank.cover_url if domain == "hdslb.com" else lambda v: image_url(v, domain)
    assert read(" \t" + prefix + "pic." + domain + "/same.jpg\n") == "https://pic." + domain + "/same.jpg"


def test_one_invalid_cover_does_not_discard_otherwise_valid_bilibili_entries():
    provider = BilibiliRank(fetch=lambda _: {"code": 0, "data": {"list": [
        {"title": "生成条目", "season_id": 1, "url": "https://www.bilibili.com/bangumi/play/ss1",
         "cover": "https://[broken/poster.jpg"}]}})
    rows = provider.read("anime")
    assert len(rows) == 1 and rows[0]["title"] == "生成条目"
    assert rows[0]["cover_url"] == ""
