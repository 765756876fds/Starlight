"""修2：语音 + UI 并发"下一首"跳两首 → next() 300ms 防抖窗口。"""
import time
from unittest.mock import MagicMock, patch

from app.music.controller import MusicController


def _make_ctrl(tmp_path):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / "debounce.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_debounce",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.lx_connected = False
    ctrl.db = db
    return ctrl


class TestNextDebounce:
    def test_rapid_second_ignored(self, tmp_path):
        """300ms 内第二次 next → 被防抖忽略（queue.next 只调一次）。"""
        ctrl = _make_ctrl(tmp_path)
        ctrl.queue = MagicMock()
        ctrl.queue.next.return_value = None
        ctrl.next()
        ctrl.next()
        ctrl.queue.next.assert_called_once()
        ctrl.db.close()

    def test_after_window_executes(self, tmp_path):
        """超过 300ms 后再次 next → 正常执行（不误伤正常连按）。"""
        ctrl = _make_ctrl(tmp_path)
        ctrl.queue = MagicMock()
        ctrl.queue.next.return_value = None
        ctrl.next()
        ctrl._last_next_ts = time.time() - 0.5
        ctrl.next()
        assert ctrl.queue.next.call_count == 2
        ctrl.db.close()

    def test_first_next_always_executes(self, tmp_path):
        """首次 next（_last_next_ts=0.0）→ 直接执行。"""
        ctrl = _make_ctrl(tmp_path)
        ctrl.queue = MagicMock()
        ctrl.queue.next.return_value = None
        ctrl.next()
        ctrl.queue.next.assert_called_once()
        ctrl.db.close()
