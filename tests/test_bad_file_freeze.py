"""坏文件播放卡死修复测试：重启拆锁不阻塞 UI + _send_command 有界 + 防重入。"""
import threading
import time
from unittest.mock import MagicMock, patch

from app.music.controller import MusicController


def _make_ctrl(tmp_path, db_name="freeze.db"):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / db_name)
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_freeze",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.db = db
    return ctrl, db


def _wait_restart_done(ctrl, timeout=3.0):
    """等待后台重启线程收尾（pending 清除）。"""
    t0 = time.time()
    while ctrl._mpv_restart_pending and time.time() - t0 < timeout:
        time.sleep(0.05)


class TestBadFileFreeze:
    def test_play_song_during_restart_not_blocked(self, tmp_path):
        """核心验收：重启进行中（start 模拟 2s），play_song 不被锁阻塞（<0.2s），
        且 pending 防御生效（不执行 loadfile）。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._mpv_restart_count = 0
        ctrl._mpv_restart_window_ts = 0.0

        def slow_start(**kw):
            time.sleep(2.0)
            return True

        ctrl.mpv.stop_process = MagicMock()
        ctrl.mpv.start.side_effect = slow_start
        ctrl.mpv.get_property.return_value = True
        ctrl._current_song = None
        ctrl._try_restart_mpv("test-slow-restart")  # 启动后台重启线程
        time.sleep(0.1)  # 确保 worker 已开始，pending=True

        song = {"id": 1, "title": "G", "path": str(tmp_path / "g.flac")}
        t0 = time.time()
        ctrl.play_song(song)
        dt = time.time() - t0
        assert dt < 0.2, f"重启期间 play_song 被锁阻塞: {dt:.2f}s"
        ctrl.mpv.loadfile.assert_not_called()
        _wait_restart_done(ctrl)
        db.close()

    def test_send_command_bounded_on_no_response(self, tmp_path):
        """mpv 不响应（管道无数据）→ _send_command 到点返回 None，不无限阻塞。"""
        from app.music.mpv import MpvController
        mpv = MpvController.__new__(MpvController)
        mpv._pipe = MagicMock()
        mpv._pipe_lock = threading.Lock()
        mpv._request_id = 0
        mpv._pipe.readline.return_value = ""  # 永不返回数据
        with patch.object(mpv, "_pipe_available", return_value=0):
            t0 = time.time()
            r = mpv._send_command("get_property", ["x"], timeout=0.5)
            dt = time.time() - t0
        assert r is None
        assert 0.3 <= dt <= 1.5, f"应到点返回 None，实际 {dt:.2f}s"

    def test_restart_no_reentry(self, tmp_path):
        """两次触发 _try_restart_mpv：pending 防重入，只有一次真重启。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._mpv_restart_count = 0
        ctrl._mpv_restart_window_ts = 0.0
        ctrl._current_song = None
        ctrl.mpv.stop_process = MagicMock()
        ctrl.mpv.start.side_effect = lambda **kw: (time.sleep(0.5), True)[1]
        ctrl.mpv.get_property.return_value = True
        ctrl._try_restart_mpv("t1")
        time.sleep(0.05)  # worker 已开始，pending=True
        ctrl._try_restart_mpv("t2")  # 防重入 → 直接 return
        assert ctrl._mpv_restart_count == 1, "第二次触发不应计数"
        _wait_restart_done(ctrl)
        assert ctrl.mpv.start.call_count == 1, "应只有一次真重启"
        db.close()

    def test_bad_file_play_ui_no_long_block(self, tmp_path):
        """坏文件链路：轮询触发重启（后台，start 慢 2s），UI 线程 play_song 不冻结。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._mpv_restart_count = 0
        ctrl._mpv_restart_window_ts = 0.0
        ctrl.mpv.stop_process = MagicMock()
        ctrl.mpv.start.side_effect = lambda **kw: (time.sleep(2.0), True)[1]
        ctrl.mpv.get_property.return_value = True
        ctrl.mpv.is_running.return_value = True
        ctrl._current_song = {"id": 26, "title": "bad", "path": "D:/bad.mp3"}
        ctrl._try_restart_mpv("pipe-fail")  # 模拟坏文件导致管道异常 → 重启
        time.sleep(0.1)
        song = {"id": 1, "title": "G", "path": str(tmp_path / "g.flac")}
        t0 = time.time()
        ctrl.play_song(song)
        dt = time.time() - t0
        assert dt < 0.3, f"UI play_song 被重启阻塞: {dt:.2f}s"
        _wait_restart_done(ctrl)
        db.close()
