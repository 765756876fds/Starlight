"""
MpvController 线程安全测试
"""
import pytest

pytestmark = pytest.mark.unit

import threading
from unittest.mock import MagicMock, patch

from app.music.mpv import MpvController


class TestMpvThreadSafety:
    def test_pipe_lock_exists(self):
        mpv = MpvController("mpv")
        assert hasattr(mpv, "_pipe_lock")
        assert isinstance(mpv._pipe_lock, type(threading.Lock()))

    def test_send_command_serialized(self):
        """两个线程同时调用 _send_command 不会串线"""
        mpv = MpvController("mpv")
        mpv._pipe = MagicMock()
        mpv._running = True

        responses = iter([
            '{"event": "some_event"}\n',
            '{"request_id": 1, "data": "v1"}\n',
            '{"request_id": 2, "data": "v2"}\n',
        ])
        mpv._pipe.readline.side_effect = lambda: next(responses, "")

        results = {}

        def call1():
            results[1] = mpv._send_command("get_property", ["prop1"])

        def call2():
            results[2] = mpv._send_command("get_property", ["prop2"])

        t1 = threading.Thread(target=call1)
        t2 = threading.Thread(target=call2)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results[1]["request_id"] == 1
        assert results[2]["request_id"] == 2
