"""Real temporary SQLite isolation + B2 attempt/outcome contracts. No network."""
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from app.music.database import MusicDatabase
from app.music.online.manager import OnlineSourceManager
from app.music.recommendation import RecommendationService
from tools.goal_shadow.contracts import Availability as A, GoalConstraints
from tools.goal_shadow_b2.capabilities import (Capability, ChartProvider, ChartRecord, Level,
    PersonalCollectionProvider, capability_current, local_attempt, source_capabilities)
from tools.goal_shadow_b2.decision import (Action, Outcome, Receipt, SyntheticExecutor,
    World, choose, simulate)
from tools.goal_shadow_b2.evaluate import from_fields, load_cases, receipts
from tools.goal_shadow_b2.sqlite_snapshot import ActiveReadSnapshot, LabOwner, OwnerReadSnapshot


def in_worker(function):
    replies=[]
    def run():
        try:
            replies.append((True,function()))
        except BaseException as exc:
            replies.append((False,exc))
    thread=threading.Thread(target=run,daemon=True,name="b2-test-reader")
    thread.start()
    thread.join(1.5)
    assert not thread.is_alive(), "bounded SQLite reader did not exit"
    success,value=replies[0]
    if not success:
        raise value
    return value


@pytest.fixture
def database(tmp_path):
    # Use the actual existing schema, but only at an isolated generated path.
    path=tmp_path/"music.db"
    db=MusicDatabase(str(path))
    pid=db.create_playlist("我喜欢的音乐")
    db.add_to_playlist(pid,dict(title="generated",artist="fixture",source_type="online",
                               source_name="fixture",track_id="track-1"))
    yield path,db,pid
    db.close()


def reader_for(mode,path):
    if mode=="A":
        return ActiveReadSnapshot(path,trusted_personal=True),None
    connection=sqlite3.connect(str(path),check_same_thread=False,isolation_level=None)
    owner=LabOwner(connection)
    return OwnerReadSnapshot(owner,trusted_personal=True),owner


@pytest.mark.parametrize("mode",["A","B"])
def test_committed_uncheckpointed_wal_is_visible(database,mode):
    path,db,pid=database
    assert Path(str(path)+"-wal").stat().st_size>0
    reader,owner=reader_for(mode,path)
    try:
        result=in_worker(reader.read)
        assert result.status=="ok" and result.total==1 and len(result.rows)==1
        assert result.rows[0].owned and result.rows[0].explicit_saved
    finally:
        if owner:
            assert owner.close()


@pytest.mark.parametrize("mode",["A","B"])
def test_writer_commits_during_reader_without_mixing_versions(database,mode):
    path,db,pid=database
    reader,owner=reader_for(mode,path)
    def pinned():
        # This is a different connection; its commit must not change the pinned read.
        db.add_to_playlist(pid,dict(title="generated second",source_type="online",source_name="fixture",track_id="track-2"))
    try:
        first=in_worker(lambda:reader.read(pinned=pinned))
        second=in_worker(reader.read)
        assert first.total==len(first.rows)==1
        assert second.total==len(second.rows)==2
        assert first.rows[0].ref==second.rows[0].ref
    finally:
        if owner:
            assert owner.close()


@pytest.mark.parametrize("mode",["A","B"])
def test_uncommitted_changes_are_not_visible(database,mode):
    path,db,pid=database
    reader,owner=reader_for(mode,path)
    db._conn.execute("BEGIN IMMEDIATE")
    db._conn.execute("INSERT INTO playlists(name,created_at) VALUES('uncommitted',0)")
    try:
        result=in_worker(reader.read)
        assert result.total==1 and result.status=="ok"
    finally:
        db._conn.rollback()
        if owner:
            assert owner.close()


@pytest.mark.parametrize("mode",["A","B"])
def test_query_timeout_and_cancel_leave_no_transaction(database,mode):
    path,db,_=database
    reader,owner=reader_for(mode,path)
    cancel=threading.Event()
    try:
        result=in_worker(lambda:reader.read(cancel=cancel,pinned=cancel.set))
        assert result.status=="unverified" and result.total is None
        cancel.clear()
        def expensive():
            connection=owner.connection if owner else None
            if connection is None:
                cancel.set()  # A still exercises real transaction cancellation.
            else:
                connection.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n").fetchone()
        timed=in_worker(lambda:reader.read(budget=.02,pinned=expensive,cancel=cancel))
        assert timed.status=="unverified"
        cancel.clear()
        assert in_worker(reader.read).status=="ok"
        if owner:
            assert not owner.connection.in_transaction
        else:
            assert reader.active_connections==0
    finally:
        if owner:
            assert owner.close()


def test_normal_ro_reader_is_bounded_when_database_exclusive_locked(tmp_path):
    path=tmp_path/"locked.db"
    writer=sqlite3.connect(path)
    writer.execute("CREATE TABLE fixture(x)")
    writer.commit()
    writer.execute("BEGIN EXCLUSIVE")
    try:
        started=time.perf_counter()
        result=in_worker(lambda:ActiveReadSnapshot(path).read(budget=.1))
        assert result.status=="unverified" and result.total is None
        assert time.perf_counter()-started<.5
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("mode",["A","B"])
def test_real_sql_progress_timeout_releases_connection_and_recovers(database,mode,monkeypatch):
    path,_,_=database
    original=sqlite3.connect
    class Expensive(sqlite3.Connection):
        slow=True
        def execute(self,sql,*args,**kwargs):
            if self.slow and sql.startswith("SELECT COUNT(*) FROM playlist_songs"):
                super().execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n").fetchone()
            return super().execute(sql,*args,**kwargs)
    monkeypatch.setattr(sqlite3,"connect",lambda *a,**k:original(*a,**dict(k,factory=Expensive)))
    reader,owner=reader_for(mode,path)
    try:
        started=time.perf_counter()
        result=in_worker(lambda:reader.read(budget=.02))
        assert result.status=="unverified" and result.total is None
        assert time.perf_counter()-started<.5
        Expensive.slow=False
        assert in_worker(reader.read).total==1
        if owner:
            assert not owner.connection.in_transaction
        else:
            assert reader.active_connections==0
    finally:
        if owner:
            assert owner.close()


def test_owner_busy_and_closed_are_not_empty(database):
    path,_,_=database
    reader,owner=reader_for("B",path)
    owner.lock.acquire()
    try:
        result=in_worker(reader.read)
        assert result.reason=="owner_lock_busy" and result.total is None
    finally:
        owner.lock.release()
    assert owner.close()
    assert in_worker(reader.read).reason=="owner_closed"


def test_owner_sqlite_lock_wait_is_bounded_and_original_timeout_restored(tmp_path):
    path=tmp_path/"exclusive.db"
    writer=sqlite3.connect(path)
    writer.execute("CREATE TABLE fixture(x)")
    writer.commit()
    connection=sqlite3.connect(path,check_same_thread=False,isolation_level=None)
    owner=LabOwner(connection)
    reader=OwnerReadSnapshot(owner)
    before=connection.execute("PRAGMA busy_timeout").fetchone()[0]
    writer.execute("BEGIN EXCLUSIVE")
    try:
        start=time.perf_counter()
        result=in_worker(lambda:reader.read(budget=.05))
        assert result.status=="unverified"
        assert time.perf_counter()-start<.5
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0]==before
        assert not connection.in_transaction
    finally:
        writer.rollback()
        writer.close()
        assert owner.close()


def test_owner_does_not_touch_existing_write_transaction(database):
    path,_,_=database
    reader,owner=reader_for("B",path)
    owner.connection.execute("BEGIN IMMEDIATE")
    try:
        result=in_worker(reader.read)
        assert result.reason=="owner_transaction_busy"
        assert owner.connection.in_transaction
    finally:
        owner.connection.rollback()
        assert owner.close()


def test_unknown_schema_and_corruption_do_not_create_empty_success(database,tmp_path):
    path,db,_=database
    db._conn.execute("PRAGMA user_version=77")
    assert in_worker(lambda:ActiveReadSnapshot(path).read()).reason=="unknown_schema_version"
    missing=tmp_path/"not-created.db"
    assert in_worker(lambda:ActiveReadSnapshot(missing).read()).reason=="database_missing"
    assert not missing.exists()
    corrupt=tmp_path/"corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    assert in_worker(lambda:ActiveReadSnapshot(corrupt).read()).status=="unverified"
    assert corrupt.read_bytes()==b"not sqlite"


@pytest.mark.parametrize("mode",["A","B"])
def test_large_playlist_is_partial_not_empty(database,mode):
    path,db,pid=database
    for i in range(8):
        db.add_to_playlist(pid,dict(title="generated",source_type="online",source_name="fixture",track_id=str(i)))
    reader,owner=reader_for(mode,path)
    reader.limit=3
    try:
        result=in_worker(reader.read)
        assert result.status=="partial" and result.total==9
        assert len(result.rows)==3 and not result.complete
    finally:
        if owner:
            assert owner.close()


def test_path_replacement_invalidates_reader_identity(tmp_path):
    path=tmp_path/"music.db"
    db=MusicDatabase(str(path))
    db.close()
    reader=ActiveReadSnapshot(path)
    assert in_worker(reader.read).status=="ok"
    # Only this test's generated files: preserve original, replace with another DB.
    path.rename(tmp_path/"original-preserved.db")
    other=MusicDatabase(str(path))
    other.close()
    assert in_worker(reader.read).reason=="database_replaced"


@pytest.mark.parametrize("mode",["A","B"])
def test_read_only_query_does_not_modify_user_rows_or_primary_file(database,mode):
    path,db,_=database
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    rows_before=db._conn.execute("SELECT COUNT(*) FROM playlist_songs").fetchone()[0]
    reader,owner=reader_for(mode,path)
    try:
        for _ in range(3):
            assert in_worker(reader.read).total==rows_before
        assert db._conn.execute("SELECT COUNT(*) FROM playlist_songs").fetchone()[0]==rows_before
        assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    finally:
        if owner:
            assert owner.close()


def test_owner_prototype_refuses_production_musicdatabase(database):
    _,db,_=database
    with pytest.raises(TypeError,match="lab_owner_only"):
        OwnerReadSnapshot(db)


class OfflineSource:
    def get_play_url(self, *args, **kwargs):
        raise AssertionError("URL request forbidden")
    def get_top_list_detail(self,*args,**kwargs):
        raise AssertionError("detail request forbidden")
    def close(self):
        pass


def test_online_identity_and_registered_resolver_are_attempt_not_playback(database):
    path,_,_=database
    manager=OnlineSourceManager()
    source=OfflineSource()
    source.name="fixture"
    manager.register(source)
    try:
        caps=source_capabilities(manager,time.monotonic())
        snapshot=in_worker(lambda:ActiveReadSnapshot(path,trusted_personal=True).read())
        provider=PersonalCollectionProvider()
        unknown=provider.propose(snapshot)
        ready=provider.propose(snapshot,sources=caps)
        assert unknown[0].level is Level.UNVERIFIED
        assert ready[0].level is Level.ATTEMPTABLE
        assert "url_not_resolved" in ready[0].basis
        assert all(c.level is not Level.PLAYBACK_CONFIRMED for c in ready)
    finally:
        manager.close()


def test_local_file_checks_are_not_decoder_confirmation(tmp_path):
    path=tmp_path/"generated.wav"
    path.write_bytes(b"not actually a valid WAV decoder stream")
    level,basis=in_worker(lambda:local_attempt(path,roots=[tmp_path],extensions={".wav"}))
    assert level is Level.ATTEMPTABLE and "decode_not_proven" in basis
    assert in_worker(lambda:local_attempt(path,roots=[],extensions={".wav"}))[0] is Level.UNVERIFIED
    path.unlink()  # Only the newly generated file in this temporary test directory.
    assert in_worker(lambda:local_attempt(path,roots=[tmp_path],extensions={".wav"}))[0] is Level.UNAVAILABLE


def test_wal_to_local_attempt_to_synthetic_outcome_without_real_playback(database,tmp_path):
    path,db,pid=database
    media=tmp_path/"generated.wav"
    media.write_bytes(b"invalid decoder bytes intentionally still attemptable")
    mid=db.insert_song(dict(path=str(media),filename=media.name,title="generated",format="wav"))
    db.add_to_playlist(pid,dict(title="generated",id=mid,source_type="local"))
    reader=ActiveReadSnapshot(path,trusted_personal=True)
    snapshot=in_worker(reader.read)
    provider=PersonalCollectionProvider()
    caps=in_worker(lambda:provider.propose(snapshot,roots=[tmp_path]))
    now=time.monotonic()
    world=World("stopped",1,1,now,movie="stopped",inventory=snapshot.status,capabilities=caps)
    decision=choose(world,now)
    assert decision.action is Action.TRY_COLLECTION and not decision.goal_satisfied
    # A plausible attempt need not decode. Only a safe failure permits fallback.
    result=simulate(decision,SyntheticExecutor([Receipt(Outcome.FAILED,no_effect_confirmed=True)]))
    assert result.status is Outcome.FAILED and not result.goal_satisfied
    before=next(c for c in caps if c.level is Level.ATTEMPTABLE)
    media.unlink()
    new_snapshot=in_worker(reader.read)
    refreshed=in_worker(lambda:provider.propose(new_snapshot,roots=[tmp_path]))
    after=next(c for c in refreshed if c.ref==before.ref)
    assert not capability_current(before,after,time.monotonic())
    stale=simulate(decision,SyntheticExecutor([Receipt(Outcome.STARTED,accepted=True,progressed=True,
        resource_current=capability_current(before,after,time.monotonic()))]))
    assert stale.status is Outcome.STALE and not stale.goal_satisfied


def test_source_replacement_invalidates_attempt_epoch(database):
    path,_,_=database
    manager=OnlineSourceManager()
    one=OfflineSource()
    one.name="fixture"
    manager.register(one)
    provider=PersonalCollectionProvider()
    snapshot=in_worker(lambda:ActiveReadSnapshot(path,trusted_personal=True).read())
    try:
        old=provider.propose(snapshot,sources=source_capabilities(manager,time.monotonic()))[0]
        two=OfflineSource()
        two.name="fixture"
        manager.register(two)
        new=provider.propose(snapshot,sources=source_capabilities(manager,time.monotonic()))[0]
        assert old.level is new.level is Level.ATTEMPTABLE
        assert not capability_current(old,new,time.monotonic())
        assert not manager._executor._threads
    finally:
        manager.close()


def test_local_access_failure_and_unsupported_extension_remain_distinct(tmp_path,monkeypatch):
    media=tmp_path/"generated.bin"
    media.write_bytes(b"fixture")
    assert in_worker(lambda:local_attempt(media,roots=[tmp_path],extensions={".wav"}))[0] is Level.UNVERIFIED
    original=Path.open
    def denied(path,*args,**kwargs):
        if path==media:
            raise PermissionError("generated denied file")
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"open",denied)
    level,basis=in_worker(lambda:local_attempt(media,roots=[tmp_path],extensions={".bin"}))
    assert level is Level.UNAVAILABLE and basis==("file_not_readable",)


def test_chart_requires_source_preparation_and_existing_service_contract():
    from types import SimpleNamespace
    manager=OnlineSourceManager()
    source=OfflineSource()
    source.name="fixture"
    manager.register(source)
    service=RecommendationService(SimpleNamespace(),SimpleNamespace())
    chart=ChartRecord("opaque-chart","fixture",time.monotonic())
    provider=ChartProvider()
    try:
        assert provider.propose([chart])[0].level is Level.UNVERIFIED
        rows=provider.propose([chart],sources=source_capabilities(manager,time.monotonic()),service=service)
        assert rows[0].level is Level.ATTEMPTABLE
        assert rows[0].preparation_required
        assert chart.concrete_tracks==0  # Macro attempt is not a claim about any song.
        more=provider.propose([chart,replace(chart,ref="other-chart")],sources=source_capabilities(manager,time.monotonic()),service=service)
        assert len(more)==1  # No second chart/song fallback engine.
    finally:
        manager.close()


@pytest.mark.parametrize("case",load_cases("dev"),ids=lambda c:c["id"])
def test_frozen_development_goal_and_outcome(case):
    world=from_fields(case["world"],time.monotonic())
    decision=choose(world,time.monotonic())
    result=simulate(decision,SyntheticExecutor(receipts(case["outcomes"])))
    assert dict(action=decision.action.value,outcome=result.status.value,attempts=result.attempts)==case["expected"]
    assert not result.executed_real_business and result.user_satisfaction=="unobserved"


def test_unknown_compatible_status_is_not_user_clarification_loop():
    world=World("stopped",1,1,time.monotonic(),movie="stopped",inventory="error")
    decision=choose(world,time.monotonic())
    assert decision.action is Action.NEEDS_VERIFICATION
    assert decision.legacy_status()=="NEEDS_CLARIFICATION"
    assert not decision.goal_satisfied


def test_only_synthetic_executor_accepted():
    with pytest.raises(TypeError,match="synthetic_executor_only"):
        simulate(choose(World("playing",1,1,time.monotonic(),current_ref="current"),time.monotonic()),lambda:None)


def test_no_progress_or_ambiguous_timeout_cannot_start_or_retry():
    now=time.monotonic()
    cap=Capability("ref","collection",Level.ATTEMPTABLE,now,exists=A.YES,owned=A.YES,content_ref="song")
    decision=choose(World("stopped",1,1,now,movie="stopped",inventory="ok",capabilities=(cap,)),now)
    assert simulate(decision,SyntheticExecutor([Receipt(Outcome.STARTED,accepted=True)])).status is Outcome.UNKNOWN
    result=simulate(decision,SyntheticExecutor([Receipt(Outcome.TIMEOUT,accepted=True)]))
    assert result.status is Outcome.TIMEOUT and result.attempts==1 and not result.goal_satisfied


def test_explicit_preference_precedes_history_and_keep_wins():
    now=time.monotonic()
    cap=Capability("ref","collection",Level.ATTEMPTABLE,now,exists=A.YES,owned=A.YES,content_ref="song",history=99)
    preferred=replace(cap,ref="explicit",content_ref="other",explicit_preference=1,history=0)
    world=World("stopped",1,1,now,movie="stopped",inventory="ok",capabilities=(cap,preferred))
    assert choose(world,now).candidates[0].ref=="explicit"
    assert choose(replace(world,constraints=GoalConstraints(keep_content=True)),now).action is Action.UNAVAILABLE
