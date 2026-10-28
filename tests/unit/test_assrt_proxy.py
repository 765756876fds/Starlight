"""Assrt 代理配置测试（批次 8）。"""
from unittest.mock import MagicMock


def _make_controller():
    from app.movie.movie_controller import MovieController
    c = MovieController.__new__(MovieController)
    c._config = MagicMock()
    return c


def test_assrt_proxy_default_backward_compat():
    """未配置 movie.assrt_proxy → 沿用历史 PROXY。"""
    c = _make_controller()
    c._config.get.return_value = "http://127.0.0.1:7897"
    p = c._assrt_proxies()
    assert p == {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}


def test_assrt_proxy_empty_direct():
    """movie.assrt_proxy="" → 直连（返回 None）。"""
    c = _make_controller()
    c._config.get.return_value = ""
    assert c._assrt_proxies() is None


def test_assrt_proxy_custom():
    """movie.assrt_proxy 自定义地址生效。"""
    c = _make_controller()
    c._config.get.return_value = "http://127.0.0.1:1080"
    p = c._assrt_proxies()
    assert p == {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"}
