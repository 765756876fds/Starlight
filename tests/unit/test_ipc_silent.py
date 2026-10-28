"""IPC 静默（playback-time=None）连续失败计数测试（批次 9）。"""
from unittest.mock import MagicMock, patch
from app.music.controller import MusicController


def _make_ctrl(tmp_path):
    db_path = str(tmp_path / "ipc_silent.db")
    cfg = {
        "database": db_path, "scan_dirs": [], "extensions": [".flac"],
        "mpv_path": "mpv", "mpv_ipc_pipe": r"\\.\pipe\test_ipc_silent",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl._mpv_fail_streak = 0
    ctrl._play_request_id = 1
    ctrl._last_loadfile_epoch = 1
    ctrl._paused_snapshot = False
    ctrl._try_restart_mpv = MagicMock()
    return ctrl


def test_single_none_no_restart(tmp_path):
    c = _make_ctrl(tmp_path)
    c._on_poll_playback_time(None)
    assert c._mpv_fail_streak == 1
    c._try_restart_mpv.assert_not_called()


def test_three_consecutive_none_triggers_restart_once(tmp_path):
    c = _make_ctrl(tmp_path)
    for _ in range(3):
        c._on_poll_playback_time(None)
    c._try_restart_mpv.assert_called_once_with("ipc-silent")
    assert c._mpv_fail_streak == 0


def test_two_none_then_valid_resets(tmp_path):
    c = _make_ctrl(tmp_path)
    c._on_poll_playback_time(None)
    c._on_poll_playback_time(None)
    assert c._mpv_fail_streak == 2
    c._on_poll_playback_time(12.5)
    assert c._mpv_fail_streak == 0
    c._try_restart_mpv.assert_not_called()


def test_zero_progress_not_failure(tmp_path):
    c = _make_ctrl(tmp_path)
    c._on_poll_playback_time(None)
    c._on_poll_playback_time(0.0)  # 0 秒是合法进度
    assert c._mpv_fail_streak == 0


def test_none_then_valid_no_residual(tmp_path):
    c = _make_ctrl(tmp_path)
    c._on_poll_playback_time(None)
    c._on_poll_playback_time(None)
    c._on_poll_playback_time(42.0)
    c._on_poll_playback_time(None)
    assert c._mpv_fail_streak == 1
    c._try_restart_mpv.assert_not_called()
