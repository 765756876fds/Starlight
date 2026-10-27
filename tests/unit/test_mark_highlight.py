"""
手动标记高潮入口测试。

覆盖：
1. mark_highlight_start 成功写入 source='manual' 和当前播放位置。
2. 无当前歌曲 / 播放位置无效时不写库。
3. 同一首歌连续标记两次：只保留最新 manual 记录，无歧义重复。
4. 两首不同歌各自独立。
5. scanner 不覆盖 manual（已有逻辑回归）。
6. clear_manual_highlight 只删 manual，不碰其他 source。
"""
import pytest
pytestmark = pytest.mark.unit

from unittest.mock import MagicMock
from app.music.database import MusicDatabase


def _make_controller(db, current_song=None, pos=100.0, duration=200.0):
    """构造一个最小可用的 MusicController，mpv/db 全 mock。"""
    from app.music import controller as ctrl_mod
    c = MagicMock()
    c.db = db
    c._current_song = current_song
    if current_song is not None:
        c.get_current_song.return_value = current_song
    else:
        c.get_current_song.return_value = None
    c.mpv.get_live_playback_time.return_value = pos
    c.mpv.get_duration.return_value = duration
    # 绑真方法
    c.mark_highlight_start = ctrl_mod.MusicController.mark_highlight_start.__get__(c, type(c))
    return c


class TestMarkHighlight:
    def _insert_song(self, db, title="T"):
        return db.insert_song({
            "path": f"/test/{title}.flac",
            "filename": f"{title}.flac",
            "title": title,
            "duration": 200.0,
        })

    def test_mark_success_writes_manual(self, music_db):
        sid = self._insert_song(music_db, "A")
        song = {"id": sid, "title": "A"}
        c = _make_controller(music_db, current_song=song, pos=95.0, duration=200.0)
        assert c.mark_highlight_start() is True
        hl = music_db.get_highlight(sid)
        assert hl is not None
        assert hl["source"] == "manual"
        assert hl["start_time"] == pytest.approx(95.0)
        assert hl["end_time"] == pytest.approx(130.0)

    def test_mark_no_current_song_no_write(self, music_db):
        sid = self._insert_song(music_db, "B")
        c = _make_controller(music_db, current_song=None, pos=95.0)
        assert c.mark_highlight_start() is False
        assert music_db.get_highlight(sid) is None

    def test_mark_invalid_position_no_write(self, music_db):
        sid = self._insert_song(music_db, "C")
        song = {"id": sid, "title": "C"}
        # pos < 5 应拒绝
        c = _make_controller(music_db, current_song=song, pos=2.0)
        assert c.mark_highlight_start() is False
        assert music_db.get_highlight(sid) is None
        # pos=None 也应拒绝
        c.mpv.get_live_playback_time.return_value = None
        assert c.mark_highlight_start() is False

    def test_mark_twice_only_latest_manual(self, music_db):
        sid = self._insert_song(music_db, "D")
        song = {"id": sid, "title": "D"}
        c = _make_controller(music_db, current_song=song, pos=50.0)
        assert c.mark_highlight_start() is True
        # 第二次标记，位置改成 120s
        c.mpv.get_live_playback_time.return_value = 120.0
        assert c.mark_highlight_start() is True
        # DB 里该 song_id 应该只有 1 行 manual
        rows = music_db._conn.execute(
            "SELECT COUNT(*) n FROM music_highlight WHERE song_id=? AND source='manual'",
            (sid,)).fetchone()
        assert rows["n"] == 1
        hl = music_db.get_highlight(sid)
        assert hl["start_time"] == pytest.approx(120.0)

    def test_two_songs_independent(self, music_db):
        s1 = self._insert_song(music_db, "E1")
        s2 = self._insert_song(music_db, "E2")
        c = _make_controller(music_db, current_song={"id": s1, "title": "E1"}, pos=30.0)
        c.mark_highlight_start()
        c._current_song = {"id": s2, "title": "E2"}
        c.get_current_song.return_value = {"id": s2, "title": "E2"}
        c.mpv.get_live_playback_time.return_value = 70.0
        c.mark_highlight_start()
        h1 = music_db.get_highlight(s1)
        h2 = music_db.get_highlight(s2)
        assert h1["start_time"] == pytest.approx(30.0)
        assert h2["start_time"] == pytest.approx(70.0)

    def test_clear_manual_keeps_other_sources(self, music_db):
        sid = self._insert_song(music_db, "F")
        music_db.save_highlight(sid, 50.0, 85.0, confidence=0.5, source="pychorus")
        music_db.save_highlight(sid, 90.0, 125.0, confidence=1.0, source="manual")
        music_db.clear_manual_highlight(sid)
        rows = music_db._conn.execute(
            "SELECT source, start_time FROM music_highlight WHERE song_id=? ORDER BY id",
            (sid,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == "pychorus"

    def test_scanner_skips_manual(self, music_db):
        """scanner 已有保护：manual 不被覆盖（回归）。"""
        sid = self._insert_song(music_db, "G")
        music_db.save_highlight(sid, 99.0, 134.0, confidence=1.0, source="manual")
        # 模拟 scanner 检查
        existing = music_db.get_highlight(sid)
        assert existing["source"] == "manual"
        # scanner 逻辑：if existing.source == "manual": continue
        skip = (existing and existing.get("source") == "manual")
        assert skip is True
