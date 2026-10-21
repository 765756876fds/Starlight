# -*- coding: utf-8 -*-
"""TgmengProvider 单测：降级契约 + 字段 schema（不依赖真实网络/密钥）。"""
import pytest
pytestmark = pytest.mark.unit
import app.movie.rank_providers as rp
from app.movie.rank_providers import TgmengProvider


class _FakeResp:
    def __init__(self, text):
        self._t = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._t.encode("utf-8")


def _provider():
    p = TgmengProvider()
    p._license = "TEST-KEY"
    return p


def test_no_license_degrades():
    """密钥缺失：get_tags/get_subjects 均返回 []，绝不抛。"""
    p = TgmengProvider()
    p._license = ""
    assert p.get_tags() == []
    assert p.get_subjects("电影") == []


def test_tags_with_license():
    """有密钥：返回预设电影分类标签。"""
    p = _provider()
    assert p.get_tags() == TgmengProvider.TAGS
    assert len(p.get_tags()) >= 3


def test_subjects_schema(monkeypatch):
    """返回 JSON → 统一 schema {title, cover, rate, url, id}。"""
    sample = {
        "code": 200,
        "data": {
            "items": [
                {"title": "测试电影A", "url": "https://x.com/a", "rank": 1},
                {"title": "测试电影B", "url": "https://x.com/b", "rank": 3,
                 "publishedAt": "2026-10-19 12:00:00"},
            ]
        },
    }
    import json
    monkeypatch.setattr(
        rp.urllib.request, "urlopen",
        lambda *a, **k: _FakeResp(json.dumps(sample)))
    p = _provider()
    subs = p.get_subjects("电影", 0, 10)
    assert len(subs) == 2
    s = subs[0]
    assert s["title"] == "测试电影A"
    assert s["cover"] == ""          # 糖果梦无封面 → 空，UI 兜底
    assert s["rate"] == "热度1"
    assert s["url"] == "https://x.com/a"
    assert s["id"] and len(s["id"]) == 12


def test_business_error_degrades(monkeypatch):
    """响应体 code != 200（业务错误也走 HTTP 200）→ []。"""
    import json
    monkeypatch.setattr(
        rp.urllib.request, "urlopen",
        lambda *a, **k: _FakeResp(json.dumps({"code": 401, "message": "授权码校验异常"})))
    p = _provider()
    assert p.get_subjects("电影") == []


def test_network_error_degrades(monkeypatch):
    """网络异常 → []，绝不抛。"""
    def boom(*a, **k):
        raise TimeoutError("timeout")
    monkeypatch.setattr(rp.urllib.request, "urlopen", boom)
    p = _provider()
    assert p.get_subjects("电影") == []
