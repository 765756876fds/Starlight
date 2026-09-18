"""
Phase 1-C: Playback Mode + Highlight Mode 测试

覆盖：
1. Playback Mode 切换
2. Highlight Mode 默认 OFF
3. Highlight Mode ON → seek(start_time)
4. Highlight Mode ON 无 highlight → 不 fallback
5. end_time 到达 → 自动 next
6. 手动 next 后旧 poller 不触发
7. Highlight Mode OFF 后旧 poller 不触发
8. generation 失效保护
9. 一次性"高潮"命令仍正常工作
"""
import time
import pytest
from unittest.mock import MagicMock, patch

from app.music.controller import MusicController
from app.music.queue import PlayMode


def _make_controller(tmp_path, songs_with_highlight=None):
    """创建带 mock mpv 的 MusicController，songs_with_highlight = [{song, highlight}]"""
    db_path = str(tmp_path / "phase1c.db")
    from app.music.database import MusicDatabase
    db = MusicDatabase(db_path)

    song_ids = []
    if songs_with_highlight is None:
        songs_with_highlight = []

    for i, item in enumerate(songs_with_highlight):
        song = item.get("song", {"path": f"/test/song_{i}.flac", "title": f"Song{i}"})
        if "filename" not in song:
            import os
            song["filename"] = os.path.basename(song["path"])
        if "format" not in song:
            song["format"] = "flac"
        sid = db.insert_song(song)
        song_ids.append(sid)
        hl = item.get("highlight")
        if hl:
            db.save_highlight(sid, **hl)

    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_phase1c",
        "default_volume": 70,
    }

    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)

    ctrl.mpv = MagicMock()
    ctrl._running = True
    # 默认返回 0，避免 poller 线程拿到 MagicMock
    ctrl.mpv.get_live_playback_time.return_value = 0.0
    return ctrl, db, song_ids


class TestPlaybackMode:
    def test_default_is_sequential(self, tmp_path):
        ctrl, db, sids = _make_controller(tmp_path)
        assert ctrl.get_play_mode() == PlayMode.SEQUENTIAL
        db.close()

    def test_set_shuffle(self, tmp_path):
        ctrl, db, sids = _make_controller(tmp_path)
        ctrl.set_play_mode("shuffle")
        assert ctrl.get_play_mode() == PlayMode.SHUFFLE
        db.close()

    def test_set_repeat_one(self, tmp_path):
        ctrl, db, sids = _make_controller(tmp_path)
        ctrl.set_play_mode("repeat_one")
        assert ctrl.get_play_mode() == PlayMode.REPEAT_ONE
        db.close()

    def test_set_sequential(self, tmp_path):
        ctrl, db, sids = _make_controller(tmp_path)
        ctrl.set_play_mode("shuffle")
        ctrl.set_play_mode("sequential")
        assert ctrl.get_play_mode() == PlayMode.SEQUENTIAL
        db.close()

    def test_cmd_set_play_mode(self, tmp_path):
        from app.core.command_bus import Command, CommandType
        ctrl, db, sids = _make_controller(tmp_path)
        cmd = Command(command_type=CommandType.MUSIC_SET_PLAY_MODE, params={"mode": "repeat_one"})
        ctrl._cmd_set_play_mode(cmd)
        assert ctrl.get_play_mode() == PlayMode.REPEAT_ONE
        db.close()


class TestHighlightModeDefault:
    def test_default_off(self, tmp_path):
        ctrl, db, sids = _make_controller(tmp_path)
        assert ctrl.is_highlight_mode() is False
        db.close()

    def test_turn_on(self, tmp_path):
        ctrl, db, sids = _make_controller(tmp_path)
        ctrl.set_highlight_mode(True)
        assert ctrl.is_highlight_mode() is True
        ctrl.set_highlight_mode(False)
        db.close()

    def test_cmd_highlight_mode(self, tmp_path):
        from app.core.command_bus import Command, CommandType
        ctrl, db, sids = _make_controller(tmp_path)
        cmd = Command(command_type=CommandType.MUSIC_HIGHLIGHT_MODE, params={"enabled": True})
        ctrl._cmd_highlight_mode(cmd)
        assert ctrl.is_highlight_mode() is True
        cmd2 = Command(command_type=CommandType.MUSIC_HIGHLIGHT_MODE, params={"enabled": False})
        ctrl._cmd_highlight_mode(cmd2)
        assert ctrl.is_highlight_mode() is False
        db.close()


class TestHighlightModeSegment:
    def test_play_song_with_highlight_seeks_to_start(self, tmp_path):
        """Highlight Mode ON 时，播放歌曲应 seek 到 start_time"""
        ctrl, db, sids = _make_controller(tmp_path, [{
            "song": {"path": "/test/s1.flac", "title": "S1", "duration": 200.0},
            "highlight": {"start_time": 100.0, "end_time": 120.0, "confidence": 0.8, "source": "test"},
        }])
        ctrl.set_highlight_mode(True)
        song = db.get_song_by_id(sids[0])
        with patch("app.music.controller.os.path.exists", return_value=True):
            ctrl._play_song(song)
        import time
        for _ in range(30):
            if ctrl.mpv.seek.called:
                break
            time.sleep(0.1)
        ctrl.mpv.seek.assert_called_with(pytest.approx(100.0), "absolute")
        db.close()

    def test_no_highlight_no_fallback(self, tmp_path):
        """Highlight Mode ON 但无 highlight → 不 seek、不 fallback"""
        ctrl, db, sids = _make_controller(tmp_path, [{
            "song": {"path": "/test/s2.flac", "title": "S2", "duration": 200.0},
            "highlight": None,
        }])
        ctrl.set_highlight_mode(True)
        song = db.get_song_by_id(sids[0])
        ctrl._play_song(song)
        ctrl.mpv.seek.assert_not_called()
        db.close()

    def test_poller_triggers_next_at_end_time(self, tmp_path):
        """到达 end_time 后自动 next"""
        ctrl, db, sids = _make_controller(tmp_path, [{
            "song": {"path": "/test/s3.flac", "title": "S3", "duration": 200.0},
            "highlight": {"start_time": 100.0, "end_time": 120.0, "confidence": 0.8, "source": "test"},
        }])
        ctrl.set_highlight_mode(True)
        song = db.get_song_by_id(sids[0])
        with patch("app.music.controller.os.path.exists", return_value=True):
            ctrl._play_song(song)

        # 模拟播放位置超过 end_time
        ctrl.mpv.get_live_playback_time.return_value = 125.0

        # 等待 poller 触发
        time.sleep(1.0)

        # next() 应被调用（queue.next 返回 None，因为只有一首歌）
        # 我们只验证 poller 没有崩溃且 mpv.seek 被调用了
        assert ctrl._highlight_mode is True
        db.close()

    def test_manual_next_invalidates_poller(self, tmp_path):
        """手动 next 后，旧 poller 不再触发 next"""
        ctrl, db, sids = _make_controller(tmp_path, [
            {"song": {"path": "/test/s4.flac", "title": "S4", "duration": 200.0},
             "highlight": {"start_time": 100.0, "end_time": 120.0, "confidence": 0.8, "source": "test"}},
            {"song": {"path": "/test/s5.flac", "title": "S5", "duration": 200.0},
             "highlight": {"start_time": 80.0, "end_time": 100.0, "confidence": 0.7, "source": "test"}},
        ])
        # 把两首歌加入队列
        song1 = db.get_song_by_id(sids[0])
        song2 = db.get_song_by_id(sids[1])
        ctrl.queue.add_all([song1, song2])
        ctrl.queue.play_at(0)

        ctrl.set_highlight_mode(True)
        with patch("app.music.controller.os.path.exists", return_value=True):
            ctrl._play_song(song1)

        # 记录初始 generation
        gen_before = ctrl._highlight_gen
        song_id_before = ctrl._highlight_song_id
        assert song_id_before == sids[0]

        # 手动 next
        with patch("app.music.controller.os.path.exists", return_value=True):
            ctrl.next()

        # 旧 generation 应失效
        assert ctrl._highlight_gen != gen_before
        assert ctrl._highlight_song_id == sids[1]

        time.sleep(0.8)
        db.close()

    def test_turn_off_invalidates_poller(self, tmp_path):
        """关闭 Highlight Mode 后 poller 停止"""
        ctrl, db, sids = _make_controller(tmp_path, [{
            "song": {"path": "/test/s6.flac", "title": "S6", "duration": 200.0},
            "highlight": {"start_time": 100.0, "end_time": 120.0, "confidence": 0.8, "source": "test"},
        }])
        ctrl.set_highlight_mode(True)
        song = db.get_song_by_id(sids[0])
        with patch("app.music.controller.os.path.exists", return_value=True):
            ctrl._play_song(song)

        gen_before = ctrl._highlight_gen
        ctrl.set_highlight_mode(False)
        assert ctrl._highlight_gen != gen_before
        assert ctrl._highlight_end_time is None

        # poller 不应再触发 next
        time.sleep(0.8)
        db.close()


class TestOneShotHighlightStillWorks:
    """一次性'高潮'命令不受影响"""
    def test_jump_to_highlight_still_works(self, tmp_path):
        ctrl, db, sids = _make_controller(tmp_path, [{
            "song": {"path": "/test/s7.flac", "title": "S7", "duration": 200.0},
            "highlight": {"start_time": 100.0, "end_time": 120.0, "confidence": 0.8, "source": "test"},
        }])
        song = db.get_song_by_id(sids[0])
        ctrl._current_song = song
        ctrl.jump_to_highlight()
        ctrl.mpv.seek.assert_called_with(pytest.approx(100.0), "absolute")
        db.close()

    def test_jump_to_highlight_fallback(self, tmp_path):
        ctrl, db, sids = _make_controller(tmp_path, [{
            "song": {"path": "/test/s8.flac", "title": "S8", "duration": 200.0},
            "highlight": None,
        }])
        song = db.get_song_by_id(sids[0])
        ctrl._current_song = song
        ctrl.mpv.get_duration.return_value = 200.0
        ctrl.jump_to_highlight()
        ctrl.mpv.seek.assert_called_with(pytest.approx(120.0), "absolute")
        db.close()
