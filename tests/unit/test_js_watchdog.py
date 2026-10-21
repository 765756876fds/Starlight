"""js_bridge watchdog 自愈测试（修3：node 退出/连续超时 → 自动 reload + 3次/5分钟上限）。"""
import pytest

pytestmark = pytest.mark.unit

import json
import threading
from unittest.mock import MagicMock, patch

from app.music.online.sources.js_source import JSMusicSource


class FakeStream:
    """stdout 迭代器：立即 EOF（模拟 node 进程退出，stdout 关闭）。"""

    def __iter__(self):
        return iter([])


def _make_src():
    """绕过 __init__（不启动 node），手动构造字段。"""
    src = JSMusicSource.__new__(JSMusicSource)
    src._proc = None
    src._lock = threading.Lock()
    src._pending = {}
    src._sources = []
    src._plugins = []
    src._ready = threading.Event()
    src._closed = False
    src._call_timeout_streak = 0
    src._auto_reload_count = 0
    src._auto_reload_window_ts = 0.0
    src._auto_reloading = False
    src._auto_reload_gave_up = False
    return src


def _patch_reload(src, ok=True):
    """手动实例属性遮蔽（patch.object 对 __new__ 实例的类方法可能不生效，手动最稳）。"""
    src.reload = MagicMock(return_value=ok)


class TestJsWatchdog:
    def test_read_loop_eof_triggers_auto_reload(self):
        """_read_loop 读到 EOF（node 退出）→ 自动 reload。"""
        src = _make_src()
        src._proc = MagicMock()
        src._proc.stdout = FakeStream()
        _patch_reload(src)
        src._read_loop()
        src.reload.assert_called_once()
        assert src._auto_reload_gave_up is False

    def test_call_timeout_streak_triggers_reload(self):
        """_call 连续 3 次超时（~45s 无响应）→ 触发自动 reload。"""
        src = _make_src()
        src._proc = MagicMock()
        _patch_reload(src)
        for _ in range(3):
            assert src._call("m", {"source": "s"}, timeout=0.01) is None
        src.reload.assert_called_once()
        assert src._call_timeout_streak == 0  # 触发后归零

    def test_single_timeout_no_reload(self):
        """单次超时（网络慢/请求久）→ 不触发 reload，streak 保留。"""
        src = _make_src()
        src._proc = MagicMock()
        _patch_reload(src)
        assert src._call("m", {"source": "s"}, timeout=0.01) is None
        src.reload.assert_not_called()
        assert src._call_timeout_streak == 1

    def test_success_resets_streak(self):
        """调用成功 → streak 清零（模拟 read_loop 响应：stdin.write 后立刻 set）。"""
        src = _make_src()
        src._proc = MagicMock()
        src._call_timeout_streak = 2

        def fake_write(s):
            rid = json.loads(s)["id"]
            with src._lock:
                box = src._pending[rid]
            box["result"] = {"ok": True, "result": "ok-payload"}
            box["event"].set()

        src._proc.stdin.write.side_effect = fake_write
        r = src._call("m", {"source": "s"}, timeout=1.0)
        assert r == "ok-payload"
        assert src._call_timeout_streak == 0

    def test_auto_reload_limit_3_per_5min(self):
        """窗口内自动恢复已达 3 次 → 放弃（gave_up），不再 reload。"""
        import time
        src = _make_src()
        src._auto_reload_count = 3
        src._auto_reload_window_ts = time.time()  # 窗口内
        _patch_reload(src)
        src._maybe_auto_reload("test-limit")
        src.reload.assert_not_called()
        assert src._auto_reload_gave_up is True

    def test_closed_skips_auto_reload(self):
        """close() 之后（_closed=True）→ 不再自动恢复。"""
        src = _make_src()
        src._closed = True
        _patch_reload(src)
        src._maybe_auto_reload("test-closed")
        src.reload.assert_not_called()
