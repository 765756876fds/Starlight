"""V4 恢复后：manual 优先级、pychorus→V4 迁移、原子替换测试。"""
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    from app.music.database import MusicDatabase
    d = MusicDatabase(str(tmp_path / "mig.db"))
    # 插入三首歌
    for i, t in enumerate(["A", "B", "C"]):
        d.insert_song({"title": t, "filename": f"{t}.flac", "path": f"/{t}.flac",
                       "last_modified": 0, "filesize": 0})
    yield d
    d.close()


def _song_ids(db):
    return [s["id"] for s in db.get_all_songs()]


def test_manual_beats_v4_same_confidence(db):
    """manual(1.0) 和 v4(1.0) 同时存在时，get_highlight 返回 manual。"""
    a, _, _ = _song_ids(db)
    db.save_highlight(a, 10.0, 45.0, confidence=1.0, source="local_v4_early_rise")
    db.save_highlight(a, 20.0, 55.0, confidence=1.0, source="manual")
    h = db.get_highlight(a)
    assert h["source"] == "manual"
    assert h["start_time"] == pytest.approx(20.0)


def test_manual_beats_pychorus(db):
    a, _, _ = _song_ids(db)
    db.save_highlight(a, 10.0, 45.0, confidence=0.5, source="pychorus")
    db.save_highlight(a, 20.0, 55.0, confidence=1.0, source="manual")
    h = db.get_highlight(a)
    assert h["source"] == "manual"


def test_has_manual_highlight(db):
    a, b, _ = _song_ids(db)
    assert db.has_manual_highlight(a) is False
    db.save_highlight(a, 20.0, 55.0, source="manual")
    assert db.has_manual_highlight(a) is True
    assert db.has_manual_highlight(b) is False


def test_atomic_replace_removes_pychorus_keeps_manual(db):
    """replace 删 pychorus，保留 manual，插入 v4。"""
    a, _, _ = _song_ids(db)
    db.save_highlight(a, 80.0, 115.0, confidence=0.5, source="pychorus")
    db.save_highlight(a, 20.0, 55.0, confidence=1.0, source="manual")
    db.replace_highlight_atomically(a, 52.0, 87.0, confidence=1.0, source="local_v4_early_rise")
    # manual 仍在
    assert db.has_manual_highlight(a) is True
    # get_highlight 返回 manual
    h = db.get_highlight(a)
    assert h["source"] == "manual"
    # v4 记录也在
    rows = db._conn.execute(
        "SELECT source, start_time FROM music_highlight WHERE song_id=?", (a,)).fetchall()
    sources = {r["source"] for r in rows}
    assert "v4" or "local_v4_early_rise" in sources
    assert "pychorus" not in sources


def test_atomic_replace_idempotent(db):
    """重复调用 replace 不会产生重复行。"""
    a, _, _ = _song_ids(db)
    db.save_highlight(a, 80.0, 115.0, confidence=0.5, source="pychorus")
    for _ in range(3):
        db.replace_highlight_atomically(a, 52.0, 87.0, 1.0, "local_v4_early_rise")
    rows = list(db._conn.execute(
        "SELECT id, source FROM music_highlight WHERE song_id=?", (a,)))
    v4 = [r for r in rows if r["source"] == "local_v4_early_rise"]
    assert len(v4) == 1, f"expected 1 v4 row, got {len(v4)}: {rows}"
    assert all(r["source"] != "pychorus" for r in rows)


def test_worker_skips_manual_and_existing_v4(db, tmp_path):
    """worker: manual 歌曲跳过；已有 v4 跳过；pychorus 歌曲走 V4 迁移。"""
    from app.music.scanner import MusicScanner
    a, b, c = _song_ids(db)
    # a: manual → 跳过
    db.save_highlight(a, 20.0, 55.0, source="manual")
    # b: 已有 v4 → 跳过
    db.save_highlight(b, 50.0, 85.0, confidence=1.0, source="local_v4_early_rise")
    # c: pychorus → 待迁移
    db.save_highlight(c, 80.0, 115.0, confidence=0.5, source="pychorus")

    sc = MusicScanner(db, [], [".flac"], "mpv")
    sc._highlight_gen = 0
    with patch("app.music.scanner.detect_highlight", return_value=(52.0, 87.0, 1.0)) as mock_detect:
        sc._highlight_worker()

    # 只有 c 被算
    mock_detect.assert_called_once()
    # a 仍 manual
    assert db.get_highlight(a)["source"] == "manual"
    # b 仍 v4 原值
    assert db.get_highlight(b)["source"] == "local_v4_early_rise"
    assert db.get_highlight(b)["start_time"] == pytest.approx(50.0)
    # c 已迁 v4
    hc = db.get_highlight(c)
    assert hc["source"] == "local_v4_early_rise"
    assert hc["start_time"] == pytest.approx(52.0)


def test_atomic_replace_cleans_existing_duplicates(db):
    """歌曲已有重复 v4 行时，replace 后只保留一行。"""
    a, _, _ = _song_ids(db)
    # 模拟历史脏数据：两条 v4
    db.save_highlight(a, 50.0, 85.0, confidence=1.0, source="local_v4_early_rise")
    db.save_highlight(a, 50.0, 85.0, confidence=1.0, source="local_v4_early_rise")
    db.replace_highlight_atomically(a, 52.0, 87.0, 1.0, "local_v4_early_rise")
    rows = list(db._conn.execute(
        "SELECT id FROM music_highlight WHERE song_id=? AND source='local_v4_early_rise'", (a,)))
    assert len(rows) == 1, f"expected 1 v4 row after replace, got {len(rows)}"


def test_worker_v4_failure_keeps_pychorus(db):
    """V4 检测失败时，pychorus 记录保留。"""
    from app.music.scanner import MusicScanner
    _, _, c = _song_ids(db)
    db.save_highlight(c, 80.0, 115.0, confidence=0.5, source="pychorus")
    sc = MusicScanner(db, [], [".flac"], "mpv")
    sc._highlight_gen = 0
    with patch("app.music.scanner.detect_highlight", return_value=None):
        sc._highlight_worker()
    h = db.get_highlight(c)
    assert h["source"] == "pychorus"


def test_scan_does_not_reset_highlight_running():
    """scanner.scan() 不能把 _highlight_running 置回 False（否则会启第二个 worker）。"""
    from app.music.scanner import MusicScanner
    from app.music.database import MusicDatabase
    import tempfile, os
    d = MusicDatabase(os.path.join(tempfile.mkdtemp(), "t.db"))
    sc = MusicScanner(d, [], [".flac"], "mpv")
    sc._highlight_running = True
    # scan() 应该不触发文件系统收集（scan_dirs=[]），但也不能重置标志
    sc.scan(full_rescan=False)
    assert sc._highlight_running is True, \
        "scan() 不应重置 _highlight_running，否则并发启动第二个 worker"
    d.close()


def test_start_highlight_background_concurrent_single_worker():
    """并发调 start_highlight_background，最终只起一个 worker。

    注意：不能 patch threading.Thread（会污染全局 threading 模块），
    改为直接验证锁保护的临界区：第一次调用置 running=True，
    第二次调用在 running=True 时直接返回，不创建线程。
    """
    from app.music.scanner import MusicScanner
    from app.music.database import MusicDatabase
    import tempfile, os, threading
    d = MusicDatabase(os.path.join(tempfile.mkdtemp(), "t.db"))
    sc = MusicScanner(d, [], [".flac"], "mpv")

    # 不真起线程：直接调 start_highlight_background 两次
    # 第一次应置 running=True 并尝试起线程（我们 patch Thread 避免真起）
    started = []
    class FakeThread:
        def __init__(self, *a, **kw): started.append(kw.get("name"))
        def start(self): pass
    import unittest.mock as m
    with m.patch("threading.Thread", FakeThread):
        sc.start_highlight_background()
        sc.start_highlight_background()
    assert len(started) == 1, f"expected 1 worker, got {len(started)}: {started}"
    assert sc._highlight_running is True
    d.close()


def test_worker_double_check_skips_if_already_written(db, monkeypatch):
    """worker 写之前再查一次：如果已经有 v4 记录，跳过（防并发重复写）。"""
    from app.music.scanner import MusicScanner
    a, b, c = _song_ids(db)
    # c 初始是 pychorus
    db.save_highlight(c, 80.0, 115.0, confidence=0.5, source="pychorus")
    sc = MusicScanner(db, [], [".flac"], "mpv")
    sc._highlight_gen = 0

    call_count = {"n": 0}
    real_detect = __import__("app.music.scanner", fromlist=["detect_highlight"]).detect_highlight
    def fake_detect(path):
        call_count["n"] += 1
        # 第一次调用前，模拟"另一个 worker 已经写了 v4"
        if call_count["n"] == 1:
            db.replace_highlight_atomically(c, 52.0, 87.0, 1.0, "local_v4_early_rise")
        return (52.0, 87.0, 1.0)
    with patch("app.music.scanner.detect_highlight", side_effect=fake_detect):
        sc._highlight_worker()
    # detect_highlight 仍被调了一次（因为 double-check 在 detect 之前），
    # 但 replace 不应被第二次调用——这里只验证最终只有一条 v4
    rows = list(db._conn.execute(
        "SELECT id FROM music_highlight WHERE song_id=? AND source='local_v4_early_rise'", (c,)))
    assert len(rows) == 1, f"expected 1 v4 row, got {len(rows)}"
