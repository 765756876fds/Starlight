"""Real WAL transaction in a worker; Qt remains responsive, no playback calls."""
import threading
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.music.database import MusicDatabase
from tools.goal_shadow_b2.sqlite_snapshot import ActiveReadSnapshot, OwnerReadSnapshot, LabOwner
from tools.goal_shadow_b2.capabilities import local_attempt
from tests.unit.test_goal_shadow_b2 import reader_for


def wait(app,predicate,timeout=1):
    until=time.monotonic()+timeout
    while not predicate() and time.monotonic()<until:
        app.processEvents()
        time.sleep(.002)
    assert predicate()


def test_wal_read_transaction_keeps_qt_responsive_and_version_consistent(tmp_path):
    app=QApplication.instance() or QApplication([])
    path=tmp_path/"music.db"
    owner=MusicDatabase(str(path))
    pid=owner.create_playlist("fixture")
    owner.add_to_playlist(pid,dict(title="generated",source_type="online",source_name="fixture",track_id="1"))
    reader=ActiveReadSnapshot(path,trusted_personal=True)
    entered,release=threading.Event(),threading.Event()
    results=[]
    def pinned():
        entered.set()
        assert release.wait(.8)
    def read():
        results.append(reader.read(budget=1,pinned=pinned))
    thread=threading.Thread(target=read,daemon=True,name="b2-qt-sql-reader")
    ticks=[]
    timer=QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda:ticks.append(time.monotonic()))
    timer.start()
    try:
        thread.start()
        wait(app,entered.is_set)
        owner.add_to_playlist(pid,dict(title="generated2",source_type="online",source_name="fixture",track_id="2"))
        wait(app,lambda:len(ticks)>=12)
        release.set()
        wait(app,lambda:not thread.is_alive())
        assert results[0].total==len(results[0].rows)==1
        assert reader.active_connections==0
    finally:
        release.set()
        thread.join(1)
        assert not thread.is_alive()
        timer.stop()
        owner.close()


def test_owner_busy_does_not_block_qt_and_closes_cleanly(tmp_path):
    app=QApplication.instance() or QApplication([])
    db=MusicDatabase(str(tmp_path/"music.db"))
    reader,owner=reader_for("B",tmp_path/"music.db")
    results=[]
    owner.lock.acquire()
    thread=threading.Thread(target=lambda:results.append(reader.read()),daemon=True)
    heartbeat=[]
    QTimer.singleShot(0,lambda:heartbeat.append(True))
    try:
        thread.start()
        wait(app,lambda:bool(heartbeat) and not thread.is_alive())
        assert results[0].reason=="owner_lock_busy"
    finally:
        owner.lock.release()
        thread.join(1)
        assert owner.close()
        db.close()


def test_sqlite_reads_reject_the_gui_thread(tmp_path):
    app=QApplication.instance() or QApplication([])
    assert app is not None
    import sqlite3
    owner=LabOwner(sqlite3.connect(":memory:"))
    try:
        for reader in (ActiveReadSnapshot(tmp_path/"absent.db"),OwnerReadSnapshot(owner)):
            with pytest.raises(RuntimeError,match="read_snapshot_requires_worker"):
                reader.read()
        with pytest.raises(RuntimeError,match="read_snapshot_requires_worker"):
            local_attempt(tmp_path/"absent.wav",roots=[tmp_path],extensions={".wav"})
    finally:
        assert owner.close()
