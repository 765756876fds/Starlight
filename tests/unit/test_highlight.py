"""
Highlight 测试：数据层 + Controller jump_to_highlight

覆盖：
1. save_highlight 写入后 confidence typeof = real（不被存为 BLOB）
2. get_highlight 返回正确记录
3. 同一 song_id 多条 highlight 按 confidence DESC 排序
4. jump_to_highlight 有 highlight → seek absolute to start_time
5. jump_to_highlight 无 highlight → fallback duration * 0.6
6. CASCADE：删除 song 后 highlight 一起删除
"""
import pytest

pytestmark = pytest.mark.unit

import os
import pytest
from unittest.mock import MagicMock, patch

from app.music.database import MusicDatabase


class TestHighlightDatabase:
    """Highlight 数据层测试"""

    def _insert_song(self, db, path="/test/song.flac", title="Test"):
        return db.insert_song({
            "path": path,
            "filename": os.path.basename(path),
            "title": title,
            "duration": 200.0,
        })

    def test_save_highlight_confidence_is_real(self, music_db):
        """save_highlight 后 confidence typeof 必须是 real，不是 blob"""
        song_id = self._insert_song(music_db)
        music_db.save_highlight(song_id, start_time=100.0, end_time=120.0,
                                confidence=0.75, source="test")

        row = music_db._conn.execute(
            "SELECT typeof(confidence) as t, confidence FROM music_highlight WHERE song_id=?",
            (song_id,)
        ).fetchone()
        assert row["t"] == "real", f"confidence is {row['t']}, expected real"
        assert row["confidence"] == pytest.approx(0.75)

    def test_save_highlight_numpy_float_not_blob(self, music_db):
        """numpy.float32 / numpy.float64 传入后也必须存为 real"""
        np = pytest.importorskip("numpy")
        song_id = self._insert_song(music_db)
        music_db.save_highlight(song_id, start_time=50.0,
                                confidence=np.float32(0.55), source="test")

        row = music_db._conn.execute(
            "SELECT typeof(confidence) as t, confidence FROM music_highlight WHERE song_id=?",
            (song_id,)
        ).fetchone()
        assert row["t"] == "real", f"confidence is {row['t']}, expected real"

    def test_get_highlight(self, music_db):
        """get_highlight 返回正确记录"""
        song_id = self._insert_song(music_db)
        music_db.save_highlight(song_id, start_time=120.0, end_time=140.0,
                                confidence=0.8, source="librosa_rms_energy")

        h = music_db.get_highlight(song_id)
        assert h is not None
        assert h["start_time"] == pytest.approx(120.0)
        assert h["end_time"] == pytest.approx(140.0)
        assert h["confidence"] == pytest.approx(0.8)
        assert h["source"] == "librosa_rms_energy"

    def test_get_highlight_returns_highest_confidence(self, music_db):
        """同一 song_id 多条 highlight 返回 confidence 最高的"""
        song_id = self._insert_song(music_db)
        music_db.save_highlight(song_id, start_time=10.0, confidence=0.2)
        music_db.save_highlight(song_id, start_time=90.0, confidence=0.9)
        music_db.save_highlight(song_id, start_time=50.0, confidence=0.5)

        h = music_db.get_highlight(song_id)
        assert h["start_time"] == pytest.approx(90.0), \
            f"expected start_time=90 (conf=0.9), got {h['start_time']}"
        assert h["confidence"] == pytest.approx(0.9)

    def test_get_highlight_none_when_empty(self, music_db):
        """无 highlight 时返回 None"""
        song_id = self._insert_song(music_db)
        assert music_db.get_highlight(song_id) is None

    def test_highlight_cascade_delete(self, music_db):
        """删除 song 后 highlight CASCADE 删除"""
        song_id = self._insert_song(music_db)
        music_db.save_highlight(song_id, start_time=100.0, confidence=0.8)

        # 确认存在
        assert music_db.get_highlight(song_id) is not None

        # 开启 FK（sqlite3 默认 OFF for each connection）
        music_db._conn.execute("PRAGMA foreign_keys = ON")
        music_db.delete_song(song_id)

        row = music_db._conn.execute(
            "SELECT COUNT(*) as cnt FROM music_highlight WHERE song_id=?",
            (song_id,)
        ).fetchone()
        assert row["cnt"] == 0, "highlight should be cascade-deleted with song"


class TestJumpToHighlight:
    """jump_to_highlight Controller 逻辑测试（mock mpv）"""

    def _make_controller(self, tmp_path, highlight_data=None):
        """创建 MusicController 并 mock mpv"""
        from app.music.controller import MusicController

        db_path = str(tmp_path / "ctrl_test.db")
        db = MusicDatabase(db_path)
        sid = db.insert_song({
            "path": "/test/ctrl_song.flac",
            "filename": "ctrl_song.flac",
            "title": "CtrlTest",
            "duration": 200.0,
        })
        if highlight_data:
            db.save_highlight(sid, **highlight_data)

        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }

        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)

        # 替换 mpv 为 MagicMock
        ctrl.mpv = MagicMock()
        ctrl._current_song = {"id": sid, "title": "CtrlTest", "duration": 200.0}
        return ctrl, db, sid

    def test_jump_to_highlight_uses_db_highlight(self, tmp_path):
        """有 highlight 时 seek 到 start_time, absolute 模式"""
        ctrl, db, sid = self._make_controller(
            tmp_path,
            highlight_data={"start_time": 123.4, "end_time": 143.4, "confidence": 0.9, "source": "test"}
        )
        ctrl.jump_to_highlight()
        ctrl.mpv.seek.assert_called_once_with(pytest.approx(123.4), "absolute")
        db.close()

    def test_jump_to_highlight_fallback_duration_60pct(self, tmp_path):
        """无 highlight 时 seek 到 duration * 0.6"""
        ctrl, db, sid = self._make_controller(tmp_path)
        ctrl.mpv.get_duration.return_value = 200.0
        ctrl.jump_to_highlight()
        ctrl.mpv.seek.assert_called_once_with(pytest.approx(120.0), "absolute")
        db.close()

    def test_jump_to_highlight_no_current_song(self, tmp_path):
        """无当前歌曲时不报错、不 seek"""
        ctrl, db, sid = self._make_controller(tmp_path)
        ctrl._current_song = None
        ctrl.jump_to_highlight()
        ctrl.mpv.seek.assert_not_called()
        db.close()
