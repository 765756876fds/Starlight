"""MEDIA-07 在线源异常回退测试（批次 11）。"""
from unittest.mock import MagicMock
from app.music.online.manager import OnlineSourceManager


def _track(name="测试", source="primary"):
    t = MagicMock()
    t.name = name
    t.source_name = source
    return t


def test_directed_source_success_priority():
    m = OnlineSourceManager()
    primary = MagicMock()
    primary.name = "primary"
    primary.get_play_url.return_value = {"url": "http://p"}
    m.register(primary)
    r = m.get_play_url(_track(source="primary"))
    assert r == {"url": "http://p"}
    primary.get_play_url.assert_called_once()


def test_directed_source_exception_falls_back():
    m = OnlineSourceManager()
    primary = MagicMock()
    primary.name = "primary"
    primary.get_play_url.side_effect = RuntimeError("boom")
    secondary = MagicMock()
    secondary.name = "secondary"
    secondary.accepts.return_value = True
    secondary.get_play_url.return_value = {"url": "http://s"}
    m.register(primary)
    m.register(secondary)
    r = m.get_play_url(_track(source="primary"))
    assert r == {"url": "http://s"}
    primary.get_play_url.assert_called_once()
    secondary.get_play_url.assert_called_once()


def test_secondary_not_called_if_not_accepts():
    m = OnlineSourceManager()
    primary = MagicMock()
    primary.name = "primary"
    primary.get_play_url.side_effect = RuntimeError("boom")
    other = MagicMock()
    other.name = "other"
    other.accepts.return_value = False
    other.get_play_url.return_value = None
    m.register(primary)
    m.register(other)
    r = m.get_play_url(_track(source="primary"))
    assert r is None
    other.get_play_url.assert_called()


def test_no_duplicate_calls():
    m = OnlineSourceManager()
    primary = MagicMock()
    primary.name = "primary"
    primary.get_play_url.side_effect = RuntimeError("boom")
    m.register(primary)
    r = m.get_play_url(_track(source="primary"))
    assert r is None
    assert primary.get_play_url.call_count == 1


def test_directed_source_returns_none_falls_back():
    """定向源正常返回 None（未找到）→ 回退到 accepts 为真的备用源。"""
    m = OnlineSourceManager()
    primary = MagicMock()
    primary.name = "primary"
    primary.get_play_url.return_value = None  # 正常未找到，非异常
    secondary = MagicMock()
    secondary.name = "secondary"
    secondary.accepts.return_value = True
    secondary.get_play_url.return_value = {"url": "http://s"}
    m.register(primary)
    m.register(secondary)
    r = m.get_play_url(_track(source="primary"))
    assert r == {"url": "http://s"}
    assert primary.get_play_url.call_count == 1
    secondary.get_play_url.assert_called_once()
