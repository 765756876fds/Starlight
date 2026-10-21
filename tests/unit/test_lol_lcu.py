"""lcu.py 线程安全测试（mock session）。"""
import pytest

pytestmark = pytest.mark.unit

import threading
import time
from unittest.mock import MagicMock

from app.lol.lcu import LCUConnection as LCUClient


def _make_client():
    c = LCUClient.__new__(LCUClient)
    c._port = 0
    c._protocol = "https"
    c._host = "127.0.0.1"
    c._connected = True
    c._request_lock = threading.Lock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.content = b'{"ok": true}'
    fake_resp.json.return_value = {"ok": True}
    c._session = MagicMock()
    c._session.get.return_value = fake_resp
    return c


def test_concurrent_request_raw_no_exception():
    c = _make_client()
    errors = []
    def worker():
        try:
            for _ in range(100):
                s, b = c.request_raw("GET", "/lol-gameflow/v1/gameflow-phase")
                assert s == 200
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors, f"errors: {errors}"
    # 共 1000 次调用
    assert c._session.get.call_count == 1000


def test_request_raw_lock_timeout_returns_within_1s():
    """锁被占用 >1s 时 request_raw 应在 ~1s 内返回 (0, None)，不无限阻塞。"""
    c = _make_client()
    # 先占住请求锁，模拟后台慢请求持有中
    assert c._request_lock.acquire(timeout=0.5)
    try:
        t0 = time.monotonic()
        status, body = c.request_raw("GET", "/lol-gameflow/v1/gameflow-phase")
        elapsed = time.monotonic() - t0
        assert (status, body) == (0, None)
        assert elapsed < 3.0      # 约 1s 返回，不是无限等
    finally:
        c._request_lock.release()
    # 锁释放后请求应恢复正常路径
    status, body = c.request_raw("GET", "/lol-gameflow/v1/gameflow-phase")
    assert status == 200
    assert body == {"ok": True}
