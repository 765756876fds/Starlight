"""mpv 崩溃/管道断自愈测试（修2：自动重启 + 3次/5分钟上限 + 歌曲恢复）。"""
import pytest

pytestmark = pytest.mark.unit

import time
from unittest.mock import MagicMock, patch

from app.music.controller import MusicController


def _make_ctrl(tmp_path, db_name="restart.db"):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / db_name)
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_restart",
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


class TestMpvRestart:
    def test_pipe_fail_streak_triggers_restart(self, tmp_path):
        """get_property 连续 3 次失败（管道断）→ _try_restart_mpv 触发重启。"""
        ctrl, db = _make_ctrl(tmp_path)
        calls = []

        def ge(prop, timeout=None):
            calls.append(1)
            if len(calls) <= 2:
                return True
            raise RuntimeError("pipe down")

        ctrl.mpv.get_property.side_effect = ge
        ctrl.mpv.is_running.return_value = True  # 进程活着 → 走 pipe-fail 分支
        ctrl.mpv.start.return_value = True
        ctrl.mpv.stop_process = MagicMock()
        try:
            ctrl.start()
            time.sleep(5.0)  # 0.8s/轮：前2轮成功，第3-5轮失败 → streak=3 → 重启
            assert ctrl.mpv.start.called, "管道连续失败应触发 mpv 重启"
            assert ctrl._mpv_restart_count >= 1
        finally:
            ctrl.stop()
            db.close()

    def test_process_dead_triggers_restart(self, tmp_path):
        """mpv 进程死亡（is_running False）→ 触发重启。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.mpv.get_property.return_value = True
        ctrl.mpv.is_running.return_value = False  # 进程没了
        ctrl.mpv.start.return_value = True
        ctrl.mpv.stop_process = MagicMock()
        try:
            ctrl.start()
            time.sleep(2.6)  # 3 轮轮询，每轮都发现进程死亡
            assert ctrl.mpv.start.called, "进程死亡应触发 mpv 重启"
        finally:
            ctrl.stop()
            db.close()

    def test_restart_limit_3_per_5min(self, tmp_path):
        """窗口内重启次数已达 3 → 放弃自愈（_mpv_gave_up），不再尝试。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._mpv_restart_count = 3
        ctrl._mpv_restart_window_ts = time.time()  # 窗口内
        ctrl._try_restart_mpv("test-limit")
        assert ctrl._mpv_gave_up is True
        ctrl.mpv.start.assert_not_called()
        db.close()

    def test_restart_resumes_local_song(self, tmp_path):
        """重启成功 + 当前歌是本地文件 → 重新 loadfile（_play_song 恢复）。"""
        ctrl, db = _make_ctrl(tmp_path)
        song = {"id": 1, "title": "X", "path": str(tmp_path / "x.flac"),
                "duration": 10.0}
        ctrl._mpv_restart_count = 0
        ctrl._mpv_restart_window_ts = 0.0  # 窗口外 → 计数重置
        ctrl._current_song = song
        ctrl._play_song = MagicMock()
        ctrl.mpv.stop_process = MagicMock()
        ctrl.mpv.start.return_value = True
        ctrl.mpv.get_property.return_value = True
        ctrl._try_restart_mpv("test-resume-local")
        _wait_restart_done(ctrl)
        assert ctrl._play_song.called, "本地歌曲应在重启后恢复播放"
        assert ctrl._mpv_restart_count == 1
        db.close()

    def test_restart_online_song_resets_last_idle(self, tmp_path):
        """当前歌是在线 URL → 不硬恢复（源已断），_last_idle=False 让轮询自动切歌。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._mpv_restart_count = 0
        ctrl._mpv_restart_window_ts = 0.0
        ctrl._current_song = {"path": "https://cdn.example/x/y.mp3", "title": "Y"}
        ctrl._last_idle = True
        ctrl._play_song = MagicMock()
        ctrl.mpv.stop_process = MagicMock()
        ctrl.mpv.start.return_value = True
        ctrl.mpv.get_property.return_value = True
        ctrl._try_restart_mpv("test-resume-online")
        _wait_restart_done(ctrl)
        assert ctrl._last_idle is False, "在线歌曲重启后应产生 idle 上升沿自动切歌"
        ctrl._play_song.assert_not_called()
        db.close()
