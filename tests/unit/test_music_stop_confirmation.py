# -*- coding: utf-8 -*-
"""第六阶段：MPV 停止确认缺口（mpv.stop() 三态语义 + stop_current 状态处理）。

覆盖：
1. MpvController.stop() 返回值约定：
   - 成功响应（error=="success"）→ True；
   - 错误响应（error!="success"）→ False；
   - 超时/无响应（_send_command 返回 None）→ None；
   - IPC 未连接/写入失败（_send_command 抛异常）→ 异常向上传播。
2. MusicController.stop_current() 三态处理：
   - 明确成功 → 清 _current_song、置 _last_idle=True、playing=False；
   - 明确失败（错误响应）→ 保留停止前状态 + 错误日志；
   - 结果未确认（超时）→ 保留停止前状态 + 错误日志；
   - 调用抛异常 → 保留停止前状态 + 错误日志；
   - 失败/未确认时队列内容与当前指针不变。

全部使用可控替身（FakeMpv 可配置 stop_result）与 patch(_send_command)，
验证可观察副作用（stop 调用、状态位、队列指针、日志内容），非 mock 自证。
"""
import pytest

pytestmark = pytest.mark.unit

from unittest.mock import patch

from app.core.state_store import StateNamespace
from app.music.controller import MusicController
from app.music.mpv import MpvController


# ===== Fake MPV（stop 三态可配） =====

class FakeMpv:
    """不发真声音的 mpv 替身；stop() 返回 stop_result（True/False/None）。"""

    def __init__(self, idle_active=False, stop_result=True):
        self.calls = []
        self._idle_active = idle_active
        self._stop_result = stop_result
        self._properties = {}

    def start(self, taskkill=True, pipe_timeout=3.0, prop_timeout=0.5):
        self.calls.append(("start",))
        return True

    def on_property_change(self, name, cb):
        self.calls.append(("on_property_change", name))

    def stop(self):
        self.calls.append(("stop",))
        return self._stop_result

    def stop_process(self):
        self.calls.append(("stop_process",))

    def is_running(self):
        return True

    def loadfile(self, filepath, mode="replace"):
        self.calls.append(("loadfile", filepath))
        return {"error": "success"}

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def get_property(self, name, timeout=2.0):
        if name == "idle-active":
            return self._idle_active
        if name == "playback-time":
            return 0.0
        if name == "duration":
            return 180.0
        if name == "pause":
            return bool(self._properties.get("pause", False))
        return self._properties.get(name)

    def set_property(self, name, value):
        self._properties[name] = value

    def get_live_playback_time(self):
        return 0.0

    def get_duration(self):
        return 180.0

    def get_current_time(self):
        return 0.0

    def get_volume(self):
        return 70

    def set_volume(self, volume):
        pass

    def is_idle(self):
        return self._idle_active

    def is_paused(self):
        return bool(self._properties.get("pause", False))

    def get_filename(self):
        return None


def _make_ctrl(tmp_path, fake, db_name="stop_confirm.db"):
    cfg = {
        "database": str(tmp_path / db_name),
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_stop_confirm",
        "default_volume": 70,
    }
    with patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        ctrl = MusicController(cfg, mpv_adapter=fake)
        ctrl.lx_connected = False
        return ctrl


def _local_song(tmp_path, song_id, title):
    p = tmp_path / f"{title}.flac"
    p.write_bytes(b"fake-flac-bytes")
    return {"id": song_id, "title": title, "path": str(p), "duration": 10.0}


def _playing_state(ctrl):
    return ctrl.state_store.get(StateNamespace.MUSIC, "playing", None)


# ===== MpvController.stop() 三态返回值 =====

class TestMpvStopReturn:
    def _adapter(self, resp, exc=None):
        a = MpvController.__new__(MpvController)
        if exc is not None:
            def _send(*args, **kwargs):
                raise exc
        else:
            def _send(*args, **kwargs):
                return resp
        a._send_command = _send
        return a

    def test_success_response_returns_true(self):
        resp = {"request_id": 1, "error": "success", "data": None}
        assert self._adapter(resp).stop() is True

    def test_error_response_returns_false(self):
        resp = {"request_id": 1, "error": "unknown command"}
        assert self._adapter(resp).stop() is False

    def test_timeout_none_response_returns_none(self):
        # IPC 超时/无响应：_send_command 返回 None → 结果未确认
        assert self._adapter(None).stop() is None

    def test_connection_error_propagates(self):
        # IPC 未连接：_send_command 抛异常 → stop() 不吞异常、向上传播
        with pytest.raises(ConnectionError):
            self._adapter(None, exc=ConnectionError("mpv IPC 未连接")).stop()


# ===== stop_current 三态状态处理 =====

class TestStopCurrentConfirmation:
    def test_success_clears_state_and_updates(self, tmp_path):
        fake = FakeMpv(stop_result=True)  # 协议层成功
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()
        ctrl._current_song = ctrl.queue.get_current()
        ctrl._last_idle = False
        ctrl._update_state(playing=True)

        ctrl.stop_current()

        assert ("stop",) in fake.calls
        assert ctrl._current_song is None
        assert ctrl._last_idle is True
        assert _playing_state(ctrl) is False
        assert not ctrl.queue.is_empty()
        assert ctrl.queue.get_current()["id"] == 1  # 指针不推进

    def test_error_response_preserves_state_and_logs(self, tmp_path):
        fake = FakeMpv(stop_result=False)  # MPV 错误响应 → 明确失败
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()
        ctrl._current_song = ctrl.queue.get_current()
        ctrl._last_idle = False
        ctrl._update_state(playing=True)

        with patch("app.music.controller.logger") as mock_logger:
            ctrl.stop_current()

        # 不伪装停止成功：状态全部保留
        assert ctrl._current_song is not None
        assert ctrl._current_song["id"] == 1
        assert ctrl._last_idle is False
        assert _playing_state(ctrl) is True
        # 队列内容与指针不变
        assert not ctrl.queue.is_empty()
        assert ctrl.queue.get_current()["id"] == 1
        # 可诊断日志
        mock_logger.error.assert_called_once()
        msg = mock_logger.error.call_args[0][0]
        assert "返回错误响应" in msg

    def test_timeout_preserves_state_and_logs(self, tmp_path):
        fake = FakeMpv(stop_result=None)  # 超时/无响应 → 结果未确认
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()
        ctrl._current_song = ctrl.queue.get_current()
        ctrl._last_idle = False
        ctrl._update_state(playing=True)

        with patch("app.music.controller.logger") as mock_logger:
            ctrl.stop_current()

        # 不假装成功，也不断言一定仍在播放 → 保留状态
        assert ctrl._current_song is not None
        assert ctrl._current_song["id"] == 1
        assert ctrl._last_idle is False
        assert _playing_state(ctrl) is True
        assert not ctrl.queue.is_empty()
        assert ctrl.queue.get_current()["id"] == 1
        mock_logger.error.assert_called_once()
        msg = mock_logger.error.call_args[0][0]
        assert "未确认" in msg

    def test_exception_preserves_state_and_logs(self, tmp_path):
        fake = FakeMpv()

        def boom():
            raise ConnectionError("mpv IPC 未连接")
        fake.stop = boom
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()
        ctrl._current_song = ctrl.queue.get_current()
        ctrl._last_idle = False
        ctrl._update_state(playing=True)

        with patch("app.music.controller.logger") as mock_logger:
            ctrl.stop_current()

        assert ctrl._current_song is not None
        assert ctrl._current_song["id"] == 1
        assert ctrl._last_idle is False
        assert _playing_state(ctrl) is True
        assert ctrl.queue.get_current()["id"] == 1
        mock_logger.error.assert_called_once()
        msg = mock_logger.error.call_args[0][0]
        assert "mpv.stop() 失败" in msg
        assert "mpv IPC 未连接" in msg
