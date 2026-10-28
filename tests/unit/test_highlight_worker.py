"""批次 17：highlight 后台 worker 代次隔离 + pychorus 重检测测试。"""
import os
from unittest.mock import MagicMock, patch
from app.music.database import MusicDatabase
from app.music.scanner import MusicScanner


def _make_scanner(tmp_path):
    db_path = str(tmp_path / "hl.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path, "scan_dirs": [], "extensions": [".flac"],
        "mpv_path": "mpv", "mpv_ipc_pipe": r"\\.\pipe\test_hl", "default_volume": 70,
    }
    with patch.object(MusicScanner, "__init__", lambda self: None):
        sc = MusicScanner()
    sc.db = db
    sc._highlight_running = False
    sc._highlight_gen = 0
    import threading
    sc._highlight_start_lock = threading.Lock()
    return sc, db


def test_pychorus_record_replaced_on_v4_success(tmp_path):
    """旧 pychorus 记录 → V4 成功 → 原子替换。"""
    sc, db = _make_scanner(tmp_path)
    sid = db.insert_song({"path": "/tmp/a.flac", "filename": "a.flac", "title": "a"})
    db.save_highlight(song_id=sid, start_time=10.0, end_time=30.0,
                      confidence=0.5, source="pychorus")
    with patch("app.music.scanner.detect_highlight", return_value=(20.0, 50.0, 0.9)):
        sc._highlight_gen = 1
        sc._highlight_worker(my_gen=1)
    row = db.get_highlight(sid)
    assert row["source"] == "local_v4_early_rise"
    assert row["start_time"] == 20.0


def test_pychorus_record_kept_on_v4_failure(tmp_path):
    """V4 失败 → 旧 pychorus 记录保留，不删。"""
    sc, db = _make_scanner(tmp_path)
    sid = db.insert_song({"path": "/tmp/b.flac", "filename": "b.flac", "title": "b"})
    db.save_highlight(song_id=sid, start_time=10.0, end_time=30.0,
                      confidence=0.5, source="pychorus")
    with patch("app.music.scanner.detect_highlight", return_value=None):
        sc._highlight_gen = 1
        sc._highlight_worker(my_gen=1)
    row = db.get_highlight(sid)
    assert row["source"] == "pychorus"
    assert row["start_time"] == 10.0


def test_old_gen_does_not_reset_running(tmp_path):
    """finally：旧 gen 退出时不复位 _highlight_running（新 gen 仍在跑）。"""
    sc, db = _make_scanner(tmp_path)
    sc._highlight_gen = 2  # 新 worker 已置 gen=2
    sc._highlight_running = True
    # 旧 worker my_gen=1 跑完
    sc._highlight_worker(my_gen=1)
    # 新 gen 仍在跑，running 不应被旧 worker 清掉
    assert sc._highlight_running is True
    assert sc._highlight_gen == 2


def test_old_gen_does_not_write_after_stale_detect(tmp_path):
    """检测完成时代次已过期 → 不写入数据库。"""
    sc, db = _make_scanner(tmp_path)
    sid = db.insert_song({"path": "/tmp/c.flac", "filename": "c.flac", "title": "c"})

    # detect_highlight 第一次调用时把 gen 改成 2（模拟新 worker 启动）
    real_gen = [1]
    def fake_detect(path):
        sc._highlight_gen = 2  # 过期
        return (20.0, 50.0, 0.9)

    with patch("app.music.scanner.detect_highlight", side_effect=fake_detect):
        sc._highlight_worker(my_gen=1)

    # 旧 worker 过期丢弃结果，DB 无记录
    assert db.get_highlight(sid) is None
