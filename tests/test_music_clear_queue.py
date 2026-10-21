"""清空队列停播测试（修5：清空队列 → 停止播放 + 清 _current_song + 失效 highlight）。"""
import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from app.music.controller import MusicController


def _make_ctrl(tmp_path, db_name="clear.db"):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / db_name)
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_clear",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.db = db
    return ctrl, db


class TestClearQueue:
    def test_clear_stops_playback_and_clears(self, tmp_path):
        """清空队列 → mpv.stop + _current_song=None + 队列空。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.queue.add({"id": 1, "title": "A", "path": "/tmp/a.flac", "duration": 10.0})
        ctrl._current_song = {"id": 1, "title": "A", "path": "/tmp/a.flac", "duration": 10.0}
        ctrl.clear_queue()
        ctrl.mpv.stop.assert_called_once()
        assert ctrl._current_song is None
        assert ctrl.queue.is_empty()
        db.close()

    def test_clear_sets_last_idle_true(self, tmp_path):
        """清空后 _last_idle=True：防止轮询把"停止后 idle"误判为播完上升沿自动切歌。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._last_idle = False  # 模拟清空前正处于播放态
        ctrl.clear_queue()
        assert ctrl._last_idle is True
        db.close()

    def test_clear_invalidates_highlight(self, tmp_path):
        """清空队列 → highlight 失效（_highlight_gen 递增、_highlight_song_id 清空）。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._highlight_gen = 5
        ctrl._highlight_song_id = 9
        ctrl.clear_queue()
        assert ctrl._highlight_gen == 6
        assert ctrl._highlight_song_id is None
        db.close()

    def test_clear_empty_queue_no_error(self, tmp_path):
        """队列本来就空 → 不抛异常。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.clear_queue()  # 不应抛
        assert ctrl._current_song is None
        db.close()

    def test_voice_command_triggers_clear_queue(self, tmp_path):
        """语音命令 MUSIC_CLEAR_QUEUE → controller.clear_queue 被调。"""
        from app.core.command_bus import Command, CommandType
        ctrl, db = _make_ctrl(tmp_path)
        with patch.object(ctrl, "clear_queue") as cq:
            ctrl.command_bus.execute(Command(
                command_type=CommandType.MUSIC_CLEAR_QUEUE,
                params={},
                source="test",
            ))
            cq.assert_called_once()
        db.close()
