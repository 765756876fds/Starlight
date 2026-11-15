"""
MpvController 线程安全测试
"""
import pytest

import threading
import json
from collections import deque
from unittest.mock import MagicMock

from app.music.mpv import MpvController

pytestmark = pytest.mark.unit


class TestMpvThreadSafety:
    def test_pipe_lock_exists(self):
        mpv = MpvController("mpv")
        assert hasattr(mpv, "_pipe_lock")
        assert isinstance(mpv._pipe_lock, type(threading.Lock()))

    @pytest.mark.parametrize("first", [1, 2])
    def test_send_command_serialized(self, first):
        """两个线程同时调用 _send_command 不会串线"""
        mpv = MpvController("mpv")
        mpv._pipe = MagicMock()
        mpv._running = True

        responses, writes = deque(), []
        first_written, contender = threading.Event(), threading.Event()
        active, peak = 0, 0

        def write(line):
            nonlocal active, peak
            message = json.loads(line)
            writes.append(message)
            active += 1
            peak = max(peak, active)
            responses.append(json.dumps({"event": "some_event"}) + "\n")
            responses.append(json.dumps({"request_id": message["request_id"],
                                         "data": message["command"][1]}) + "\n")
            if message["command"][1] == f"prop{first}":
                first_written.set()
                assert contender.wait(2)

        def read():
            nonlocal active
            line = responses.popleft() if responses else ""
            if line and "request_id" in json.loads(line):
                active -= 1
            return line

        mpv._pipe.write.side_effect = write
        mpv._pipe.readline.side_effect = read
        results, errors = {}, []

        def call(which):
            try:
                if which != first:
                    assert first_written.wait(2)
                    contender.set()
                results[which] = mpv._send_command("get_property", [f"prop{which}"])
            except Exception as error:
                errors.append(error)

        t1 = threading.Thread(target=call, args=(1,))
        t2 = threading.Thread(target=call, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not t1.is_alive() and not t2.is_alive()
        assert not errors
        # Request IDs follow lock acquisition, not Thread.start() order.
        assert results[first]["request_id"] == 1
        assert results[3 - first]["request_id"] == 2
        assert results[1]["data"] == "prop1" and results[2]["data"] == "prop2"
        assert [row["command"][1] for row in writes] == [f"prop{first}", f"prop{3 - first}"]
        assert peak == 1 and active == 0 and not responses
