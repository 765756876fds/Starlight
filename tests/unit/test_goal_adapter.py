"""Temporary DB + real controller contract; fake MPV, no network/user data."""

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import threading
from unittest.mock import patch

import pytest

from app.music.goal_adapter import GoalAdapter, AdapterResult, Prepared
from tests.unit.test_goal_admission import setup as setup
from tests.unit.test_goal_shadow_b2 import in_worker


def adapter(c, **kwargs):
    return GoalAdapter(c, isolation_root=Path(c.db_path).parent, enabled=True, **kwargs)


def chart_source(c):
    from tools.goal_execute_b3.lab import LocalSource

    source = LocalSource([])
    source._plugins = [dict(name=source.name, status="ok", capabilities=dict(charts=True))]
    c.online_manager.register(source)
    return source


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_default_disabled_does_not_construct_owner_or_read_databases(setup):
    c, _, _, _, _ = setup
    a = GoalAdapter(c, isolation_root=Path(c.db_path).parent)
    with patch.object(c, "goal_state", side_effect=AssertionError("must not read")):
        assert a.run(a.begin()).status == "disabled"
    assert not hasattr(c, "_goal_admission")


def test_prepare_does_not_initialize_preferences_call_choices_or_load(setup):
    c, p, _, _, _ = setup
    source = chart_source(c)
    memory = c.music_memory.path
    assert not memory.exists()
    before = fingerprint(c.db_path)
    a = adapter(c)
    with patch.object(c.recommendations, "choices", side_effect=AssertionError("network/write")):
        result = in_worker(lambda: a.prepare(a.begin()))
    assert isinstance(result, Prepared) and result.decision.plan == "TRY_CHART"
    assert not memory.exists() and fingerprint(c.db_path) == before
    assert not source.calls and not p.loads


def test_explicit_artist_selects_local_and_negative_excludes_it(setup):
    c, _, song, _, _ = setup
    chart_source(c)
    c.db.update_song(song["id"], dict(artist="已确认歌手"))
    assert c.music_memory.set_preference("artist", "已确认歌手", 1)
    before = fingerprint(c.music_memory.path)
    a = adapter(c)
    prepared = in_worker(lambda: a.prepare(a.begin()))
    assert prepared.decision.plan == "TRY_COLLECTION"
    assert prepared.decision.reason == "verified_local_explicit_preference"
    assert fingerprint(c.music_memory.path) == before
    c.music_memory.set_preference("artist", "已确认歌手", -1)
    assert in_worker(lambda: a.prepare(a.begin())).decision.plan == "TRY_CHART"


@pytest.mark.parametrize(
    "change", ["manual", "member", "source", "new_goal", "expired", "file", "preference"]
)
def test_changes_after_prepare_reject_without_old_load(setup, change):
    c, p, song, pid, mid = setup
    a = adapter(c)
    t = a.begin()
    prepared = in_worker(lambda: a.prepare(t))
    assert prepared.decision.plan == "TRY_COLLECTION"
    if change == "manual":
        c.play_song(song)
    elif change == "member":
        c.db.remove_from_playlist(pid, mid)
    elif change == "source":
        chart_source(c)
    elif change == "new_goal":
        a.begin()
    elif change == "expired":
        prepared = replace(
            prepared, state=replace(prepared.state, captured=prepared.state.captured - 2)
        )
    elif change == "file":
        Path(song["path"]).write_bytes(b"different generated content")
    else:
        c.music_memory.set_preference("artist", "new preference", 1)
    before = list(p.loads)
    result = in_worker(lambda: a.execute(t, prepared))
    assert result.status == "rejected_stale", result
    assert p.loads == before


def test_pending_unknown_forbids_second_plan_even_after_new_request(setup):
    c, p, _, _, _ = setup
    p.reply = None
    a = adapter(c)
    result = in_worker(lambda: a.run(a.begin()))
    assert result.status == "unknown_effects" and result.receipt.effects_started
    before = list(p.loads)
    assert in_worker(lambda: a.run(a.begin())).reason == "previous_admission_unresolved"
    assert p.loads == before and len(before) == 1


def test_ack_without_progress_times_out_without_second_load(setup):
    c, p, _, _, _ = setup
    p.progress = False
    a = adapter(c)
    result = in_worker(lambda: a.run(a.begin(), budget=0.12))
    assert result.status == "unknown_effects" and result.receipt.mpv_accepted
    assert len(p.loads) == 1


@pytest.mark.parametrize("close", [False, True])
def test_concurrent_preparation_new_request_is_bounded_and_invalidates_old(setup, close):
    c, p, _, _, _ = setup
    a = adapter(c)
    old = a.begin()
    entered, release = threading.Event(), threading.Event()
    capture = a.resources.capture
    results = []

    def slow(*args):
        entered.set()
        assert release.wait(0.7)
        return capture(*args)

    with patch.object(a.resources, "capture", side_effect=slow):
        task = threading.Thread(target=lambda: results.append(a.prepare(old)))
        task.start()
        try:
            assert entered.wait(1)
            newer = a.begin()
            assert in_worker(lambda: a.prepare(newer)).reason == "adapter_busy"
            if close:
                a.close()
        finally:
            release.set()
            task.join(1)
    assert not task.is_alive() and isinstance(results[0], AdapterResult)
    assert results[0].status in ("needs_verification", "rejected_stale") and not p.loads


@pytest.mark.parametrize("kind", ["corrupt", "future", "partial"])
def test_bad_preference_snapshot_preserved_and_not_treated_as_empty(setup, kind):
    c, p, _, _, _ = setup
    path = c.music_memory.path
    if kind == "corrupt":
        path.write_bytes(b"not sqlite")
    else:
        c.music_memory.profile()
        with sqlite3.connect(path) as db:
            if kind == "future":
                db.execute("PRAGMA user_version=99")
            else:
                db.executemany(
                    "INSERT INTO preferences VALUES('music','artist',?,1)",
                    [(str(i),) for i in range(513)],
                )
    before = fingerprint(path)
    a = adapter(c)
    assert in_worker(lambda: a.run(a.begin())).status == "needs_verification"
    assert not p.loads and fingerprint(path) == before


def test_outside_isolation_and_non_plain_goal_never_touch_owner(setup, tmp_path):
    c, p, _, _, _ = setup
    a = GoalAdapter(c, isolation_root=tmp_path / "unrelated", enabled=True)
    assert in_worker(lambda: a.run(a.begin())).status == "needs_verification"
    a = adapter(c)
    assert in_worker(lambda: a.run(a.begin("LISTEN_MUSIC:风格"))).status == "rejected_unavailable"
    assert not p.loads and not hasattr(c, "_goal_admission")


def test_wal_members_committed_after_checkpoint_visible_without_write(setup):
    c, _, _, _, _ = setup
    with c.db._lock:
        c.db._conn.execute("PRAGMA journal_mode=WAL")
        c.db._conn.execute("PRAGMA wal_autocheckpoint=0")
        c.db._conn.execute("UPDATE music SET artist='WAL artist'")
        c.db._conn.commit()
    c.music_memory.set_preference("artist", "WAL artist", 1)
    wal = Path(str(c.db_path) + "-wal")
    before = fingerprint(wal)
    chart_source(c)
    a = adapter(c)
    prepared = in_worker(lambda: a.prepare(a.begin()))
    assert prepared.decision.plan == "TRY_COLLECTION" and fingerprint(wal) == before


def test_registered_methods_without_plugin_chart_evidence_remain_unknown(setup):
    c, p, _, pid, mid = setup
    source = chart_source(c)
    source._plugins = []
    c.db.remove_from_playlist(pid, mid)
    a = adapter(c)
    result = in_worker(lambda: a.run(a.begin()))
    assert result.status == "needs_verification" and not source.calls and not p.loads


def test_metadata_change_and_database_owner_busy_do_not_fake_empty_collection(setup):
    c, p, _, _, _ = setup
    source = chart_source(c)
    a = adapter(c)
    ticket = a.begin()
    prepared = in_worker(lambda: a.prepare(ticket))
    source._plugins[0]["capabilities"]["charts"] = False
    assert in_worker(lambda: a.execute(ticket, prepared)).status == "rejected_stale"
    c.db._lock.acquire()
    try:
        result = in_worker(lambda: a.run(a.begin()))
        assert result.status == "needs_verification" and not p.loads
    finally:
        c.db._lock.release()


@pytest.mark.parametrize("change", ["manual", "new_request"])
def test_race_immediately_before_b4_admission_rejected_by_its_owner(setup, change):
    c, p, song, _, _ = setup
    a = adapter(c)
    ticket = a.begin()
    prepared = in_worker(lambda: a.prepare(ticket))
    admit = c.admit_music_goal

    def race(request):
        if change == "manual":
            c.play_song(song)
        else:
            a.begin()
        return admit(request)

    with patch.object(c, "admit_music_goal", side_effect=race) as calls:
        result = in_worker(lambda: a.execute(ticket, prepared))
    assert result.status == "rejected_stale" and not result.receipt.effects_started
    assert calls.call_count == 1 and len(p.loads) == (1 if change == "manual" else 0)


def test_exception_after_admission_never_allows_automatic_second_attempt(setup):
    c, p, _, _, _ = setup
    a = adapter(c)
    admit = c.admit_music_goal

    def interrupted(request):
        admit(request)
        raise OSError("generated interrupted return")

    with patch.object(c, "admit_music_goal", side_effect=interrupted):
        result = in_worker(lambda: a.run(a.begin()))
    assert result.status == "unknown_effects" and result.receipt.effects_started
    assert in_worker(lambda: a.run(a.begin())).reason == "previous_admission_unresolved"
    assert len(p.loads) == 1


def test_unknown_actual_state_does_not_become_stopped(setup):
    c, p, song, _, _ = setup
    c.play_song(song)
    p.props["pause"] = None
    a = adapter(c)
    assert in_worker(lambda: a.run(a.begin())).status == "needs_verification"
    assert len(p.loads) == 1


def test_recommender_closed_after_preparation_invalidates_old_chart_plan(setup):
    c, p, _, _, _ = setup
    chart_source(c)
    a = adapter(c)
    ticket = a.begin()
    prepared = in_worker(lambda: a.prepare(ticket))
    assert prepared.decision.plan == "TRY_CHART"
    c.recommendations.cancel(close=True)
    result = in_worker(lambda: a.execute(ticket, prepared))
    assert result.status == "rejected_stale"
    assert not p.loads


def test_budget_cannot_extend_b4_deadline_contract(setup):
    c, p, _, _, _ = setup
    a = adapter(c)
    ticket = a.begin()
    prepared = in_worker(lambda: a.prepare(ticket))
    with patch.object(c, "admit_music_goal", side_effect=AssertionError("must not admit")):
        assert (
            in_worker(lambda: a.execute(ticket, prepared, budget=45)).status
            == "rejected_unavailable"
        )
    assert not p.loads
