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
