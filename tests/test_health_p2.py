"""体检修复 P2：event_bus shutdown 禁发 + publish_async 有界池 + execute_async 异常记录 + 裸 except 收敛行为不变。"""
import pytest

pytestmark = pytest.mark.unit


import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest


# ============ event_bus ============

def _make_bus():
    from app.core.event_bus import EventBus, Event, EventType
    bus = EventBus()
    return bus, Event, EventType


def test_publish_after_shutdown_ignored():
    """shutdown 后 publish 不生效：订阅者不被调用、事件不进历史。"""
    bus, Event, _ = _make_bus()
    got = []
    bus.subscribe("test.evt", lambda e: got.append(e))
    bus.publish(Event(event_type="test.evt"))
    assert len(got) == 1

    bus.shutdown()
    bus.publish(Event(event_type="test.evt"))
    bus.publish(Event(event_type="test.evt"))
    assert len(got) == 1  # shutdown 后不再分发
    hist = bus.get_history(event_type="test.evt", limit=100)
    assert len(hist) == 1  # 历史也不再追加


def test_publish_async_thread_bound():
    """publish_async 线程数有上限（max_workers=4）。"""
    bus, Event, _ = _make_bus()
    assert bus._async_executor._max_workers == 4
    # 行为验证：慢 handler，多发事件，同时活跃线程 ≤ 4
    active = []
    lock = threading.Lock()
    count = [0]
    peak = [0]

    def slow_handler(_e):
        with lock:
            count[0] += 1
            cur = count[0]
            peak[0] = max(peak[0], cur)
        time.sleep(0.05)
        with lock:
            count[0] -= 1

    bus.subscribe("test.slow", slow_handler)
    for _ in range(16):
        bus.publish_async(Event(event_type="test.slow"))
    # 等待全部完成
    deadline = time.time() + 5
    while count[0] > 0 and time.time() < deadline:
        time.sleep(0.02)
    assert peak[0] <= 4


def test_publish_async_after_shutdown_ignored():
    """shutdown 后 publish_async 不再提交线程池（不产生新线程）。"""
    bus, Event, _ = _make_bus()
    bus.shutdown()
    # 不应抛异常（submit 已关池）
    bus.publish_async(Event(event_type="test.evt"))


# ============ command_bus ============

def test_execute_async_exception_logged():
    """execute_async 子线程异常被捕获记录，不静默死。"""
    from app.core.command_bus import CommandBus, Command, CommandType
    bus = CommandBus()

    def boom(_cmd):
        raise ValueError("boom")

    bus.register(CommandType.MUSIC_NEXT, boom)
    with patch("app.core.command_bus.logger") as lg:
        bus.execute_async(Command(command_type=CommandType.MUSIC_NEXT))
        # 等线程跑完
        deadline = time.time() + 2
        while time.time() < deadline:
            time.sleep(0.02)
    lg.error.assert_called_once()
    assert "boom" in str(lg.error.call_args.args[2])


def test_execute_sync_still_raises():
    """同步 execute 保持原语义（异常继续上抛，不被吞）。"""
    from app.core.command_bus import CommandBus, Command, CommandType
    bus = CommandBus()

    def boom(_cmd):
        raise ValueError("boom-sync")

    bus.register(CommandType.MUSIC_NEXT, boom)
    with pytest.raises(ValueError):
        bus.execute(Command(command_type=CommandType.MUSIC_NEXT))


# ============ 裸 except 收敛行为不变 ============

def test_lx_client_post_json_fallback():
    """lx_client._post：JSON 解析失败 → 返回 {"raw": text}（行为不变）。"""
    from app.music.lx_client import LxMusicClient
    c = LxMusicClient()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.side_effect = ValueError("not json")
    resp.text = "plain"
    with patch("requests.post", return_value=resp):
        out = c._post("/api/player/toggle")
    assert out == {"raw": "plain"}


def test_lx_client_is_connected_fallback():
    """lx_client.is_connected：请求异常 → False（行为不变）。"""
    from app.music.lx_client import LxMusicClient
    c = LxMusicClient()
    with patch("requests.get", side_effect=OSError("conn refused")):
        assert c.is_connected() is False


def test_source_manager_js_import_fallback(tmp_path):
    """source_manager.import_from_json：非 JSON 文件 → 降级提取 @name（行为不变）。"""
    from app.music.source_manager import MusicSourceManager
    js = tmp_path / "test_source.js"
    js.write_text("// @name 测试源\n// @version 1.0\n", encoding="utf-8")
    m = MusicSourceManager(str(tmp_path))  # 隔离目录，绝不碰真实 config/music_sources.json
    src = m.import_from_json(str(js))
    assert src.name == "测试源"
