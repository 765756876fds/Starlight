"""高潮模式连续切歌自动 seek 回归测试。"""
import os, time, pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

from app.music.database import MusicDatabase
from app.music.controller import MusicController


def _make(tmp_path, n=3):
    db_path = str(tmp_path / "hl.db")
    db = MusicDatabase(db_path)
    songs = []
    for i in range(n):
        fake_path = str(tmp_path / f"song{i}.flac")
        with open(fake_path, "wb") as f:
            f.write(b"\x00" * 100)
        sid = db.insert_song({
            "path": fake_path,
            "filename": f"song{i}.flac",
            "title": f"Song{i}",
            "duration": 200.0,
        })
        db.save_highlight(sid, start_time=100.0 + i*10, end_time=130.0 + i*10,
                          confidence=0.9, source="test")
        songs.append({"id": sid, "title": f"Song{i}", "path": fake_path, "duration": 200.0})
    cfg = {
        "database": db_path, "scan_dirs": [], "extensions": [".flac"],
        "mpv_path": "mpv", "mpv_ipc_pipe": r"\\.\pipe\test_hl", "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.mpv.loadfile.return_value = {"error": "success"}
    return ctrl, db, songs


def _absolute_seeks(mpv):
    return [c for c in mpv.seek.call_args_list if c.args and c.args[1] == "absolute"]


def test_mode_on_every_new_song_seeks(tmp_path):
    """开启高潮模式后，连续播两首有 highlight 的歌，每首都应触发一次 absolute seek。"""
    ctrl, db, songs = _make(tmp_path)
    ctrl.set_highlight_mode(True)
    ctrl.play_song(songs[0])
    time.sleep(1.0)
    n_after_0 = len(_absolute_seeks(ctrl.mpv))

    ctrl.play_song(songs[1])
    time.sleep(1.0)
    n_after_1 = len(_absolute_seeks(ctrl.mpv))

    ctrl.stop()
    db.close()
    assert n_after_0 >= 1, f"song0 应触发至少 1 次高潮 seek，实际 {n_after_0}"
    assert n_after_1 >= 2, f"song1 后应累计至少 2 次高潮 seek，实际 {n_after_1}"


def test_mode_off_no_autoseek(tmp_path):
    """模式关闭时，播歌不自动 seek 到高潮。"""
    ctrl, db, songs = _make(tmp_path)
    ctrl.set_highlight_mode(False)
    ctrl.play_song(songs[0])
    time.sleep(1.0)
    n = len(_absolute_seeks(ctrl.mpv))
    ctrl.stop()
    db.close()
    assert n == 0, f"模式关闭不应自动 seek，实际 {n}"


def test_mode_then_off_stops_autoseek(tmp_path):
    """开→播歌→关→播歌：第二首不应再自动 seek。"""
    ctrl, db, songs = _make(tmp_path)
    ctrl.set_highlight_mode(True)
    ctrl.play_song(songs[0])
    time.sleep(0.8)
    n_after_0 = len(_absolute_seeks(ctrl.mpv))

    ctrl.set_highlight_mode(False)
    ctrl.play_song(songs[1])
    time.sleep(0.8)
    n_after_1 = len(_absolute_seeks(ctrl.mpv))

    ctrl.stop()
    db.close()
    assert n_after_0 >= 1
    assert n_after_1 == n_after_0, f"关闭模式后不应新增 seek（{n_after_0} → {n_after_1}）"


def test_single_jump_still_works_when_mode_off(tmp_path):
    """模式关闭时，单次 jump_to_highlight 仍应 seek。"""
    ctrl, db, songs = _make(tmp_path)
    ctrl.set_highlight_mode(False)
    ctrl._current_song = songs[0]
    ctrl.jump_to_highlight()
    ctrl.mpv.seek.assert_called()
    ctrl.stop()
    db.close()


def test_mode_on_no_highlight_falls_back_to_60pct(tmp_path):
    """模式开启但歌曲无预计算 highlight 时，应 seek 到 60% 位置（与单次一致），
    而不是静默整首从头播。"""
    ctrl, db, songs = _make(tmp_path)
    # 删掉 song0 的 highlight
    db._conn.execute("DELETE FROM music_highlight WHERE song_id=?", (songs[0]["id"],))
    db._conn.commit()
    ctrl.mpv.get_duration.return_value = 200.0
    ctrl.set_highlight_mode(True)
    ctrl.play_song(songs[0])
    time.sleep(1.0)
    seeks = [c for c in ctrl.mpv.seek.call_args_list if c.args and c.args[1] == "absolute"]
    ctrl.stop()
    db.close()
    assert len(seeks) >= 1, f"无 highlight 时应 seek 到 60%，实际 seek 次数 {len(seeks)}"
    assert abs(seeks[-1].args[0] - 120.0) < 1.0, f"应 seek 到 120s (60% of 200)，实际 {seeks[-1].args[0]}"
