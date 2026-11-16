"""B1 contracts, actual SQLite schemas and in-memory controller read boundaries."""
from dataclasses import replace
import hashlib
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from app.music.database import MusicDatabase
from app.music.preferences import PreferenceStore
from app.music.queue import PlayQueue
from tools.goal_shadow.contracts import (Action, Availability as A, CapabilityEvidence,
    Goal, GoalConstraints, Resource, WorldStateSnapshot)
from tools.goal_shadow.decision import decide
from tools.goal_shadow.readonly import (LiveReadAdapter, artist_ref, cached_charts,
    collections, immutable_database, preference_summary)


def proof(state=A.YES, *, source="isolated_proof", age=0):
    return CapabilityEvidence(state, source, time.monotonic()-age, True)


def world(state="stopped", resources=()):
    now = time.monotonic()
    return WorldStateSnapshot(now, now, (1, 2), state, proof(), proof(), "current",
                              proof(), 1, tuple(resources), "stopped", True,
                              inventory=proof(), catalog=proof())


def item(ref="saved", kind="collection", *, availability=A.YES, preference=0, history=0):
    return Resource(ref, kind, proof(), proof(availability), proof(), count=1,
                    explicit_preference=preference, historical_preference=history,
                    content_ref=ref+"-song")


def decision(w, constraints=GoalConstraints(), generation=None):
    return decide(Goal(constraints=constraints), w, now=time.monotonic(),
                  current_generation=w.generation if generation is None else generation)


def capture_in_worker(adapter, budget=.6):
    """Honor the real read contract even when an earlier test created Qt."""
    replies = []
    def read():
        try:
            replies.append((True, adapter.capture(budget)))
        except BaseException as exc:
            replies.append((False, exc))
    thread = threading.Thread(target=read, daemon=True, name="b1-test-read")
    thread.start()
    thread.join(budget+.2)
    assert not thread.is_alive(), "bounded test reader failed to exit"
    succeeded, value = replies[0]
    if not succeeded:
        raise value
    return value


@pytest.mark.parametrize("state,expected", [("playing", Action.NO_OP), ("paused", Action.RESUME_MUSIC)])
def test_current_session_is_preserved(state, expected):
    result = decision(world(state, [item()]))
    assert result.action is expected
    assert result.goal_satisfied is (expected is Action.NO_OP)
    assert not result.execution_performed


def test_stopped_with_proven_personal_resource():
    assert decision(world(resources=[item()])).action is Action.PLAY_PREFERRED


def test_chart_after_personal_resource_proven_failed():
    result = decision(world(resources=[item(availability=A.NO), item("chart", "chart")]))
    assert result.action is Action.PLAY_CHART
    assert result.selected_ref == "chart"
    assert not result.goal_satisfied


def test_no_available_content():
    assert decision(world(resources=[item(availability=A.NO)])).action is Action.UNAVAILABLE


def test_membership_is_not_playability():
    result = decision(world(resources=[item(availability=A.UNKNOWN)]))
    assert result.action is Action.NEEDS_CLARIFICATION
    assert not result.candidates[0].eligible


def test_keep_content_excludes_replacements():
    result = decision(world(resources=[item()]), GoalConstraints(keep_content=True))
    assert result.action is Action.UNAVAILABLE
    assert all(not p.eligible for p in result.candidates)


def test_change_cannot_claim_no_op_or_replay_same_item():
    same = replace(item(), content_ref="current")
    result = decision(world("playing", [same]), GoalConstraints(change_content=True))
    assert result.action is Action.UNAVAILABLE
    assert not result.goal_satisfied


def test_change_with_new_proven_resource():
    assert decision(world("playing", [item()]), GoalConstraints(change_content=True)).action is Action.PLAY_PREFERRED


@pytest.mark.parametrize("mutation", ["generation", "age", "consistency"])
def test_snapshot_invalidation(mutation):
    w = world("playing")
    if mutation == "age":
        w = replace(w, captured_at=w.captured_at-5)
    if mutation == "consistency":
        w = replace(w, consistent=False)
    result = decision(w, generation=(1, 9) if mutation == "generation" else None)
    assert result.action is Action.NEEDS_CLARIFICATION
    assert result.expired and not result.goal_satisfied


@pytest.mark.parametrize("state", ["playing", "unknown"])
def test_no_new_cross_media_policy(state):
    w = replace(world("paused", [item()]), movie_state=state)
    result = decision(w)
    assert result.action is Action.NEEDS_CLARIFICATION
    assert result.reason == "cross_media_policy_not_authorized"


def test_no_op_does_not_interrupt_existing_movie_either():
    assert decision(replace(world("playing"), movie_state="playing")).action is Action.NO_OP


def test_explicit_preference_before_history():
    result = decision(world(resources=[item("explicit", preference=1), item("history", history=1000)]))
    assert result.selected_ref == "explicit"


def test_explicit_dislike_is_not_just_low_rank():
    assert decision(world(resources=[item(preference=-1, history=100)])).action is Action.UNAVAILABLE


def test_equivalent_personal_targets_need_evidence():
    assert decision(world(resources=[item("a"), item("b")])).action is Action.NEEDS_CLARIFICATION


@pytest.mark.parametrize("state,reliable", [(A.YES, False), (A.UNKNOWN, True)])
def test_unreliable_or_unknown_never_upgrades(state, reliable):
    row = replace(item(), playable=replace(proof(state), reliable=reliable))
    assert decision(world(resources=[row])).action is Action.NEEDS_CLARIFICATION


def test_stale_resource_is_not_selected():
    row = replace(item(), playable=proof(age=5))
    assert decision(world(resources=[row])).action is Action.NEEDS_CLARIFICATION


def test_unproven_tag_not_fabricated():
    result = decision(world(resources=[item()]), GoalConstraints(required_tags=frozenset({"calm"})))
    assert result.action is Action.UNAVAILABLE
    assert "required_tags_unproven" in result.candidates[0].reasons


def seeded_db(path):
    db = MusicDatabase(str(path))
    pid = db.create_playlist("我喜欢的音乐")
    assert db.add_to_playlist(pid, dict(title="PRIVATE_TITLE_DO_NOT_LOG", artist="Test Artist",
                                      source_type="online", source_name="fixture", track_id="song-1"))
    db.close()


def test_real_schema_readonly_no_new_files_or_changes(tmp_path):
    path = tmp_path/"music.db"
    seeded_db(path)
    before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    rows, status = collections(path, now=time.monotonic(), deadline=time.monotonic()+1)
    after = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    assert before == after
    assert status.state is A.YES
    assert len(rows) == 1 and rows[0].count == 1
    assert rows[0].owned.state is A.YES
    assert rows[0].playable.state is A.UNKNOWN
    assert "PRIVATE_TITLE" not in repr(rows)


def test_read_connection_rejects_writes(tmp_path):
    path = tmp_path/"music.db"
    seeded_db(path)
    with immutable_database(path, time.monotonic()+1) as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM playlists")


def test_bounded_inventory_does_not_disguise_unread_tail_as_empty(tmp_path):
    path = tmp_path/"music.db"
    db = MusicDatabase(str(path))
    for i in range(65):
        db.create_playlist("isolated-"+str(i))
    db.close()
    rows, status = collections(path, now=time.monotonic(), deadline=time.monotonic()+1)
    assert not rows
    assert status.state is A.UNKNOWN
    assert status.reason == "inventory_truncated"


def test_active_wal_refused_not_silently_ignored(tmp_path):
    path = tmp_path/"music.db"
    db = MusicDatabase(str(path))
    try:
        db.create_playlist("fixture")
        rows, status = collections(path, now=time.monotonic(), deadline=time.monotonic()+1)
        assert not rows and status.state is A.UNKNOWN
    finally:
        db.close()


@pytest.mark.parametrize("case", ["missing", "corrupt"])
def test_database_failure_no_creation_or_recovery(tmp_path, case):
    path = tmp_path/"music.db"
    if case == "corrupt":
        path.write_bytes(b"not a sqlite database")
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    rows, status = collections(path, now=time.monotonic(), deadline=time.monotonic()+1)
    assert not rows and status.state is (A.NO if case == "missing" else A.UNKNOWN)
    assert before == {p.name:p.read_bytes() for p in tmp_path.iterdir()}


def test_real_preference_schema_excludes_automatic_plays(tmp_path):
    path = tmp_path/"preferences.db"
    store = PreferenceStore(path)
    store.set_preference("artist", "Test Artist", 1)
    store.record(dict(title="generated", artist="Test Artist", id=1), "manual")
    store.record(dict(title="generated-auto", artist="Other", id=2), "recommendation")
    before = path.read_bytes()
    notes, explicit, inferred = preference_summary(path, now=time.monotonic(), deadline=time.monotonic()+1)
    assert explicit[artist_ref("Test Artist")] == 1
    assert inferred[artist_ref("Test Artist")] == 1
    assert artist_ref("Other") not in inferred
    assert "reliable_history_count=1" in notes
    assert before == path.read_bytes()


def controller_fixture(tmp_path, state="playing"):
    controller = SimpleNamespace(_lock=threading.Lock(), _play_request_id=3,
        _current_song=dict(id=1, path="isolated-media"), _is_switching_track=False,
        queue=PlayQueue(), db_path=tmp_path/"missing.db", music_memory=None,
        recommendations=None, online_manager=None)
    controller.queue.add(controller._current_song)
    controller.queue.play_at(0)
    class Port:
        def sample(self, deadline):
            return {"idle-active": state == "stopped", "pause": state == "paused",
                    "path": "isolated-media", "time-pos": 1.0, "time-pos-after": 1.1}
    return controller, Port()


def test_adapter_uses_real_queue_without_consuming_or_mutating(tmp_path):
    controller, port = controller_fixture(tmp_path)
    before = controller.queue.snapshot(controller._current_song)
    adapter = LiveReadAdapter(controller, port, movie_reader=lambda: SimpleNamespace(state="stopped"))
    captured = capture_in_worker(adapter)
    result = decide(Goal(), captured, now=time.monotonic(), current_generation=adapter.generation())
    assert result.action is Action.NO_OP
    assert captured.queue.state is A.YES
    assert before == controller.queue.snapshot(controller._current_song)
    assert not list(tmp_path.iterdir())


def test_generation_changes_mid_snapshot_are_rejected(tmp_path):
    controller, port = controller_fixture(tmp_path)
    original = port.sample
    def changed(deadline):
        controller._play_request_id += 1
        return original(deadline)
    port.sample = changed
    captured = capture_in_worker(LiveReadAdapter(controller, port))
    assert not captured.consistent
    assert decision(captured).expired


def test_queue_revision_changes_mid_snapshot_are_rejected(tmp_path):
    controller, port = controller_fixture(tmp_path)
    original = port.sample
    def changed(deadline):
        controller.queue.add(dict(id=2, path="other-isolated-media"))
        return original(deadline)
    port.sample = changed
    captured = capture_in_worker(LiveReadAdapter(controller, port))
    assert not captured.consistent


def test_decoder_path_mismatch_is_unknown(tmp_path):
    controller, port = controller_fixture(tmp_path)
    controller._current_song["path"] = "different"
    captured = capture_in_worker(LiveReadAdapter(controller, port))
    assert captured.music_state == "unknown"


def test_idle_false_before_decoded_position_is_not_playing(tmp_path):
    controller, port = controller_fixture(tmp_path)
    port.sample = lambda deadline: {"idle-active":False, "pause":False,
        "path":"isolated-media", "time-pos":None, "time-pos-after":.1}
    captured = capture_in_worker(LiveReadAdapter(controller, port))
    assert captured.music_state == "unknown"
    assert captured.music.state is A.UNKNOWN


def test_controller_lock_contention_is_bounded(tmp_path):
    controller, port = controller_fixture(tmp_path)
    controller._lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            capture_in_worker(LiveReadAdapter(controller, port))
        assert time.monotonic()-started < .2
    finally:
        controller._lock.release()


def test_real_recommender_cache_metadata_is_not_playability():
    from app.music.recommendation import RecommendationService
    manager = SimpleNamespace(_life_lock=threading.Lock(), _source_versions={"fixture":1})
    controller = SimpleNamespace(online_manager=manager)
    controller.recommendations = RecommendationService(controller, SimpleNamespace())
    controller.recommendations._catalog = (
        (id(manager), (("fixture",1),)),
        {"fixture":[{"lists":[{"id":"rank-1", "title":"PRIVATE_CHART"}]}]},
    )
    controller.recommendations._catalog_at = time.monotonic()
    rows, status = cached_charts(controller, time.monotonic())
    assert status.state is A.YES and len(rows) == 1
    assert rows[0].playable.state is A.UNKNOWN
    assert "PRIVATE_CHART" not in repr(rows)
    manager._source_versions["fixture"] = 2
    rows, status = cached_charts(controller, time.monotonic())
    assert not rows and status.state is A.UNKNOWN


def test_query_port_waits_for_observed_progress_not_fixed_sleep():
    from tools.goal_shadow.mpv_read_port import MpvReadPort
    port = object.__new__(MpvReadPort)
    values = iter([1.0, 1.0, 1.1])
    calls = []
    def read(key, deadline):
        calls.append(key)
        return next(values) if key == "time-pos" else {"path":"generated", "pause":False, "idle-active":False}[key]
    port.get_property = read
    sample = port.sample(time.monotonic()+.5)
    assert sample["time-pos"] == 1 and sample["time-pos-after"] == 1.1
    assert calls.count("time-pos") == 3
