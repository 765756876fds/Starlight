"""真实临时 SQLite 与受控后台任务的关闭顺序回归。"""
import sqlite3
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.music.database import MusicDatabase
from app.music.scanner import MusicScanner


def test_close_waits_until_get_all_songs_finishes_fetching(tmp_path):
    db = MusicDatabase(str(tmp_path / "read-close.db"))
    db.insert_song({"path": "/test/a.flac", "filename": "a.flac", "title": "A"})
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    real = db._conn
    errors, rows = [], []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            entered.set()
            assert release.wait(3)
            return self.cursor.fetchall()

    class Connection:
        def execute(self, sql, *args):
            cursor = real.execute(sql, *args)
            if sql.startswith("SELECT * FROM music ORDER BY"):
                return Cursor(cursor)
            return cursor

        def close(self):
            real.close()
            closed.set()

    db._conn = Connection()

    def read():
        try:
            rows.extend(db.get_all_songs())
        except Exception as exc:
            errors.append(exc)

    def close():
        closing.set()
        db.close()

    reader = threading.Thread(target=read)
    closer = threading.Thread(target=close)
    try:
        reader.start()
        assert entered.wait(2)
        closer.start()
        assert closing.wait(2)
        assert not closed.wait(.1), "查询结果尚未取完，连接已经被关闭"
    finally:
        release.set()
        reader.join(2)
        if closer.ident is not None:
            closer.join(2)
        db.close()
    assert not reader.is_alive() and not closer.is_alive()
    assert errors == []
    assert [row["title"] for row in rows] == ["A"]
    assert closed.is_set()


def test_controller_stop_waits_for_highlight_reader_before_database_close(tmp_path):
    from app.music.controller import MusicController

    with patch("app.music.controller.LxMusicClient"):
        ctrl = MusicController({
            "database": str(tmp_path / "controller.db"), "scan_dirs": [],
            "extensions": [".flac"], "mpv_path": "missing-test-mpv",
        }, mpv_adapter=MagicMock())
    entered, release, stopped, stopping = (threading.Event() for _ in range(4))
    original = ctrl.db.get_all_songs
    workers, alive_at_stop = [], []

    def read(*args, **kwargs):
        workers.append(threading.current_thread())
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    ctrl.db.get_all_songs = read

    def stop():
        stopping.set()
        ctrl.stop()
        alive_at_stop.append(workers[0].is_alive())
        stopped.set()

    stopper = threading.Thread(target=stop)
    try:
        ctrl.scanner.start_highlight_background()
        assert entered.wait(2)
        stopper.start()
        assert stopping.wait(2)
        assert not stopped.wait(.1), "控制器停止后仍有扫描线程访问数据库"
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
        if stopper.ident is not None:
            stopper.join(2)
        ctrl.stop()
        ctrl.db.close()
    assert not stopper.is_alive()
    assert alive_at_stop == [False]


def test_highlight_stop_timeout_does_not_write_late_result(tmp_path, monkeypatch):
    db = MusicDatabase(str(tmp_path / "late-result.db"))
    db.insert_song({"path": "/test/a.flac", "filename": "a.flac"})
    scanner = MusicScanner(db, [], mpv_path="missing-test-mpv")
    entered, release = threading.Event(), threading.Event()
    workers, writes = [], []

    def detect(path):
        workers.append(threading.current_thread())
        entered.set()
        assert release.wait(3)
        return 10., 30., .9

    monkeypatch.setattr("app.music.scanner.detect_highlight", detect)
    monkeypatch.setattr(db, "replace_highlight_atomically", lambda **kw: writes.append(kw))
    try:
        scanner.start_highlight_background()
        assert entered.wait(2)
        assert scanner.stop_highlight_background(timeout=.02) is False
        assert scanner.stop_highlight_background(timeout=.02) is False
        scanner.start_highlight_background()
        assert len(workers) == 1, "旧检测仍未退出时不得启动第二个检测"
        db.close()
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
        db.close()
    assert all(not worker.is_alive() for worker in workers)
    assert writes == [], "超时后的旧检测结果不得再写入已关闭的数据库"
    assert scanner.stop_highlight_background(timeout=.1) is True


def test_scanner_can_restart_and_calculate_pending_songs_after_stop(tmp_path, monkeypatch):
    db = MusicDatabase(str(tmp_path / "restart.db"))
    scanner = MusicScanner(db, [], mpv_path="missing-test-mpv")
    calls = []

    def detect(path):
        calls.append(path)
        return 10., 30., .9

    monkeypatch.setattr("app.music.scanner.detect_highlight", detect)
    try:
        assert scanner.stop_highlight_background(timeout=.1) is True
        sid = db.insert_song({"path": "/test/a.flac", "filename": "a.flac"})
        scanner.start_highlight_background()
        scanner._highlight_thread.join(2)
        assert db.get_highlight(sid)["start_time"] == 10.
        assert scanner.stop_highlight_background(timeout=.1) is True
        sid2 = db.insert_song({"path": "/test/b.flac", "filename": "b.flac"})
        scanner.start_highlight_background()
        scanner._highlight_thread.join(2)
        assert db.get_highlight(sid2)["start_time"] == 10.
        assert calls == ["/test/a.flac", "/test/b.flac"]
    finally:
        if hasattr(scanner, "stop_highlight_background"):
            scanner.stop_highlight_background(timeout=2)
        db.close()


def test_stop_timeout_is_bounded_while_worker_commit_lock_is_busy(tmp_path):
    db = MusicDatabase(str(tmp_path / "busy-commit.db"))
    scanner = MusicScanner(db, [], mpv_path="missing-test-mpv")
    done, results = threading.Event(), []

    def stop():
        results.append(scanner.stop_highlight_background(timeout=.02))
        done.set()

    scanner._highlight_start_lock.acquire()
    stopper = threading.Thread(target=stop)
    try:
        stopper.start()
        assert done.wait(1), "等待提交锁时也必须遵守停止超时，不能无限等待"
        assert results == [False]
    finally:
        scanner._highlight_start_lock.release()
        stopper.join(2)
        scanner.stop_highlight_background(timeout=.1)
        db.close()


def test_get_all_songs_after_close_raises_a_managed_database_error(tmp_path):
    db = MusicDatabase(str(tmp_path / "closed.db"))
    db.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        db.get_all_songs()
