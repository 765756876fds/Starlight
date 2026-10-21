"""修1：REPEAT_ONE 单曲循环 + 文件消失 → 卡死 修复测试。

- queue.skip_current：REPEAT_ONE 下顺序取下一首（逃生路径）
- controller._play_song 文件不存在分支：REPEAT_ONE → 立即跳；连续 3 次 → 停播
"""
import pytest

pytestmark = pytest.mark.unit

import os
from unittest.mock import MagicMock, patch

from app.music.queue import PlayQueue, PlayMode
from app.music.controller import MusicController


def _make_ctrl(tmp_path):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / "skip.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_skip",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.lx_connected = False
    ctrl.db = db
    return ctrl


class TestQueueSkipCurrent:
    def test_skip_current_repeat_one_advances(self):
        """REPEAT_ONE 下 skip_current 顺序取下一首（不再循环回当前）。"""
        q = PlayQueue()
        q.add({"path": "a"})
        q.add({"path": "b"})
        q.add({"path": "c"})
        q.set_play_mode(PlayMode.REPEAT_ONE)
        q._current_index = 1
        nxt = q.skip_current()
        assert nxt == {"path": "c"}
        assert q._current_index == 2

    def test_skip_current_tail_returns_none(self):
        """REPEAT_ONE 队列到尾 → 返回 None（不循环回当前）。"""
        q = PlayQueue()
        q.add({"path": "a"})
        q.add({"path": "b"})
        q.set_play_mode(PlayMode.REPEAT_ONE)
        q._current_index = 1
        assert q.skip_current() is None
        assert q._current_index == 1  # 不越界

    def test_skip_current_repeat_all_wraps(self):
        """REPEAT_ALL 队列到尾 → 回绕到 0（循环语义保留）。"""
        q = PlayQueue()
        q.add({"path": "a"})
        q.add({"path": "b"})
        q.set_play_mode(PlayMode.REPEAT_ALL)
        q._current_index = 1
        nxt = q.skip_current()
        assert nxt == {"path": "a"}
        assert q._current_index == 0

    def test_skip_current_empty_queue(self):
        q = PlayQueue()
        q.set_play_mode(PlayMode.REPEAT_ONE)
        assert q.skip_current() is None


class TestPlaySongMissingFile:
    def test_missing_file_repeat_one_skips(self, tmp_path):
        """REPEAT_ONE + 文件不存在 → 第一次即调 skip_current 逃生。"""
        ctrl = _make_ctrl(tmp_path)
        ctrl.queue = PlayQueue()
        ctrl.queue.add({"path": "a"})
        ctrl.queue.add({"path": "b"})
        ctrl.queue.set_play_mode(PlayMode.REPEAT_ONE)
        ctrl.queue._current_index = 0
        ctrl.queue.skip_current = MagicMock(wraps=ctrl.queue.skip_current)
        ctrl._play_song({"id": 1, "path": str(tmp_path / "gone.flac")})
        ctrl.queue.skip_current.assert_called_once()
        assert ctrl._missing_file_streak == 1

    def test_missing_file_streak_stops_after_3(self, tmp_path):
        """连续 3 次文件不存在 → stop() 停播（限次保护）。"""
        ctrl = _make_ctrl(tmp_path)
        ctrl.queue = PlayQueue()
        ctrl.queue.add({"path": "a"})
        ctrl.queue.set_play_mode(PlayMode.REPEAT_ONE)
        ctrl.stop = MagicMock()
        missing = {"id": 1, "path": str(tmp_path / "gone.flac")}
        for _ in range(2):
            ctrl._play_song(missing)
        assert ctrl.stop.call_count == 0  # 前 2 次未触顶
        ctrl._play_song(missing)          # 第 3 次 → 停播
        ctrl.stop.assert_called_once()
        assert ctrl._missing_file_streak == 0  # 计数复位

    def test_missing_file_sequential_no_skip(self, tmp_path):
        """非 REPEAT_ONE（顺序模式）→ 不调 skip_current（现有上升沿机制自愈）。"""
        ctrl = _make_ctrl(tmp_path)
        ctrl.queue = PlayQueue()
        ctrl.queue.add({"path": "a"})
        ctrl.queue.set_play_mode(PlayMode.SEQUENTIAL)
        ctrl.queue.skip_current = MagicMock()
        ctrl._play_song({"id": 1, "path": str(tmp_path / "gone.flac")})
        ctrl.queue.skip_current.assert_not_called()
        assert ctrl._missing_file_streak == 1

    def test_play_song_success_resets_streak(self, tmp_path):
        """播放成功 → 连续失败计数重置为 0。"""
        ctrl = _make_ctrl(tmp_path)
        ctrl.queue = PlayQueue()
        ctrl.queue.add({"path": "a"})
        good = tmp_path / "ok.flac"
        good.write_bytes(b"fake")
        ctrl._missing_file_streak = 2
        ctrl._play_song({"id": 1, "path": str(good)})
        assert ctrl._missing_file_streak == 0
        ctrl.mpv.loadfile.assert_called_once_with(str(good))
        ctrl.db.close()
