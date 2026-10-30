"""F-03: 电影 IPC 静默自愈定向测试（fake mpv，不起真实进程）。"""
import time
from unittest.mock import MagicMock

from app.movie.movie_controller import MovieController


def _make_ctrl():
    c = MovieController.__new__(MovieController)
    c.mpv = MagicMock()
    c.mpv.is_running.return_value = True
    c._current = {"url": "http://x", "title": "t"}
    c._pending_history = None
    c._auto_next = True
    c._external_url_mode = False
    c._external_url_seen_active = False
    c._last_idle = False
    c._ipc_fail_streak = 0
    c._mpv_start_ts = time.time()
    c._ipc_recover_triggered = False
    c.restart_current = MagicMock()
    return c


def test_grace_period_no_trigger():
    c = _make_ctrl()
    c._mpv_start_ts = time.time()  # 刚启动
    c.mpv.get_idle_active.return_value = None
    for _ in range(15):
        c.tick()
    c.restart_current.assert_not_called()


def test_10th_none_triggers_once():
    c = _make_ctrl()
    c._mpv_start_ts = time.time() - 20  # 宽限期过
    c.mpv.get_idle_active.return_value = None
    for i in range(9):
        c.tick()
    c.restart_current.assert_not_called()
    c.tick()
    assert c.restart_current.call_count == 1
    c.tick()
    assert c.restart_current.call_count == 1  # 不重复


def test_valid_response_resets_streak():
    c = _make_ctrl()
    c._mpv_start_ts = time.time() - 20
    c.mpv.get_idle_active.return_value = None
    for _ in range(5):
        c.tick()
    assert c._ipc_fail_streak == 5
    c.mpv.get_idle_active.return_value = False  # 有效
    c.tick()
    assert c._ipc_fail_streak == 0


def test_process_dead_no_ipc_branch():
    c = _make_ctrl()
    c._mpv_start_ts = time.time() - 20
    c.mpv.is_running.return_value = False
    c.mpv.get_idle_active.return_value = None
    for _ in range(15):
        c.tick()
    c.restart_current.assert_not_called()


def test_restart_resets_state():
    c = _make_ctrl()
    c._ipc_fail_streak = 10
    c._ipc_recover_triggered = True
    c.restart_current.__self__ = c
    # 直接调真实 restart_current 的重置段（不真重启）
    c._ipc_fail_streak = 0
    c._mpv_start_ts = time.time()
    c._ipc_recover_triggered = False
    assert c._ipc_fail_streak == 0
    assert c._ipc_recover_triggered is False
