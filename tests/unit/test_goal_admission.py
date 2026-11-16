"""Authority + real controller/DB concurrency; fake IPC, never user data."""

from dataclasses import replace
from pathlib import Path
import threading
import time
from unittest.mock import patch

import pytest

from app.music.controller import MusicController
from app.music.goal_admission import AdmissionStatus as S, GoalRequest, SnapshotError
from app.music.online.manager import OnlineSourceManager
from app.core.command_bus import CommandBus
from app.core.event_bus import EventBus
from app.core.state_store import StateStore
from tests.unit.test_goal_shadow_b2 import in_worker


class Player:
    def __init__(self):
        self.loads = []
        self.props = {"pause": False}
        self.progress = True
        self.reply = {"error": "success"}
        self.before_send = None

    def loadfile(self, path):
        self.loads.append(path)
        return self.reply

    def loadfile_guarded(self, path, deadline, current):
        if self.before_send:
            self.before_send()
        return self.loadfile(path) if current() else None

    def get_property(self, name, **kwargs):
        if name == "path":
            return self.loads[-1] if self.loads else None
        if name == "idle-active":
            return not bool(self.loads)
        if name == "time-pos":
            return time.perf_counter() if self.loads and self.progress else 0
        if name == "playlist":
            return (
                [{"id": len(self.loads), "current": True, "filename": self.loads[-1]}]
                if self.loads
                else []
            )
        return self.props.get(name)

    def set_property(self, name, value):
        self.props[name] = value
        return {"error": "success"}

    def play(self):
        self.set_property("pause", False)

    def pause(self):
        self.set_property("pause", True)

    def on_property_change(self, *args):
        pass

    def stop_process(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    player = Player()
    monkeypatch.setattr("app.music.controller.get_command_bus", CommandBus)
    monkeypatch.setattr("app.music.controller.get_event_bus", EventBus)
    monkeypatch.setattr("app.music.controller.get_state_store", StateStore)
    with patch("app.music.controller.LxMusicClient") as lx:
        lx.return_value.is_connected.return_value = False
        c = MusicController(
            dict(database=str(tmp_path / "music.db"), scan_dirs=[]), mpv_adapter=player
        )
    c._running = True
    c.state_store.set("movie", "playing", False)
    c.online_manager = OnlineSourceManager(max_workers=1)
    c.set_movie_pauser(lambda: True)
    media = tmp_path / "generated.wav"
    media.write_bytes(b"generated fixture bytes, native decoding tested separately")
    song = dict(path=str(media), title="generated", filename=media.name, format="wav")
    song["id"] = c.db.insert_song(song)
    pid = c.db.create_playlist("我喜欢的音乐")
    assert c.db.add_to_playlist(pid, dict(song, source_type="local"))
    member_id = c.db.get_playlist_songs(pid)[0]["id"]
    before_threads = set(threading.enumerate())
    yield c, player, song, pid, member_id
    c.stop()
    c.event_bus.shutdown(timeout=0.5)
    for thread in set(threading.enumerate()) - before_threads:
        thread.join(1)
        assert not thread.is_alive()
    c.db.close()


def request(c, pid, mid, plan="TRY_COLLECTION", operation="goal"):
    token = in_worker(c.goal_state)
    member = in_worker(lambda: c.goal_member(pid, mid)) if plan == "TRY_COLLECTION" else None
    return GoalRequest(operation, plan, token, time.monotonic() + 2, member)


def admit(c, req):
    return in_worker(lambda: c.admit_music_goal(req))


def unchanged(c, player):
    return (
        c.queue.snapshot(c.get_current_song()),
        c.db._conn.total_changes,
        c.get_current_song(),
        list(player.loads),
    )


def test_accepted_is_not_started_and_duplicate_has_one_load(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    value = admit(c, req)
    assert value.status is S.ACCEPTED and value.mpv_accepted and not value.safe_to_fallback
    assert admit(c, req) == value and len(p.loads) == 1
    assert in_worker(lambda: c.observe_music_goal(req.operation_id)).status is S.ACCEPTED
    assert in_worker(lambda: c.observe_music_goal(req.operation_id)).status is S.STARTED


@pytest.mark.parametrize(
    "change", ["member_delete", "member_replace", "resource", "source", "request", "manual_pause"]
)
def test_stale_authority_rejected_before_any_side_effect(setup, change):
    c, p, song, pid, mid = setup
    req = request(c, pid, mid)
    if change == "member_delete":
        c.db.remove_from_playlist(pid, mid)
    elif change == "member_replace":
        with c.db._lock, c.db._conn:
            c.db._conn.execute("UPDATE playlist_songs SET track_id='changed' WHERE id=?", (mid,))
    elif change == "resource":
        Path(song["path"]).write_bytes(b"changed bytes")
    elif change == "source":
        c.online_manager._source_versions["fixture"] = 1
    elif change == "request":
        with c._lock:
            c._play_request_id += 1
    else:
        c.pause()
    before = unchanged(c, p)
    value = admit(c, req)
    assert value.status is S.REJECTED_STALE and not value.effects_started
    assert unchanged(c, p) == before


def test_cancel_and_deadline_reject_without_effect(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    before = unchanged(c, p)
    req.cancel.set()
    assert admit(c, req).status is S.REJECTED_STALE
    assert (
        admit(c, replace(req, cancel=threading.Event(), deadline=time.monotonic() - 1)).status
        is S.REJECTED_STALE
    )
    assert unchanged(c, p) == before


def test_db_busy_and_error_are_not_empty_or_accepted(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    before = unchanged(c, p)
    c.db._lock.acquire()
    try:
        start = time.monotonic()
        result = admit(c, req)
        assert result.status is S.FAILED_NO_EFFECT and time.monotonic() - start < 0.5
        assert not result.safe_to_fallback
    finally:
        c.db._lock.release()
    with patch.object(c.db, "playback_member_lease", side_effect=SnapshotError("database_closed")):
        assert admit(c, req).status is S.FAILED_NO_EFFECT
    assert unchanged(c, p) == before


def test_controller_busy_returns_bounded_without_mutating(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    before = unchanged(c, p)
    c._lock.acquire()
    try:
        start = time.monotonic()
        value = admit(c, req)
        assert value.status is S.REJECTED_UNAVAILABLE and time.monotonic() - start < 0.5
    finally:
        c._lock.release()
    assert unchanged(c, p) == before


def test_queue_effects_before_ipc_refusal_forbid_fallback(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    p.reply = None
    value = admit(c, req)
    assert value.effects_started and not value.mpv_accepted and not value.safe_to_fallback
    assert c.queue.size() > 0
    second = replace(req, operation_id="other")
    assert admit(c, second).reason == "previous_operation_unresolved"
    assert len(p.loads) == 1


def test_no_progress_cannot_confirm_and_unknown_retains_exclusive_claim(setup):
    c, p, _, pid, mid = setup
    p.progress = False
    req = request(c, pid, mid)
    assert admit(c, req).status is S.ACCEPTED
    for _ in range(2):
        assert in_worker(lambda: c.observe_music_goal(req.operation_id)).status is not S.STARTED
    req.cancel.set()
    assert in_worker(lambda: c.observe_music_goal(req.operation_id)).status is S.REJECTED_STALE


def test_read_failure_before_effect_can_use_another_fresh_plan(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    before = unchanged(c, p)
    with patch("app.music.goal_admission.file_identity", side_effect=PermissionError()):
        value = admit(c, req)
    assert value.status is S.FAILED_NO_EFFECT and value.safe_to_fallback
    assert unchanged(c, p) == before
    assert admit(c, replace(req, operation_id="retry")).status is S.ACCEPTED


def test_manual_new_song_waiting_for_admission_invalidates_before_mpv(setup):
    c, p, song, pid, mid = setup
    req = request(c, pid, mid)
    entered, release = threading.Event(), threading.Event()
    p.before_send = lambda: (entered.set(), release.wait(0.8))
    answers = []
    worker = threading.Thread(target=lambda: answers.append(c.admit_music_goal(req)))
    worker.start()
    assert entered.wait(1)
    manual = threading.Thread(target=lambda: c.play_song(song))
    manual.start()
    end = time.monotonic() + 0.5
    while c._goal_admission.revision == req.expected.revision and time.monotonic() < end:
        time.sleep(0.002)
    assert c._goal_admission.revision != req.expected.revision
    p.before_send = None
    release.set()
    worker.join(1)
    manual.join(1)
    assert not worker.is_alive() and not manual.is_alive()
    assert answers[0].status is S.REJECTED_STALE and answers[0].effects_started
    assert len(p.loads) == 1  # only the newer manual operation was sent


def test_database_removal_serializes_with_collection_admission(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    entered, release, removed = threading.Event(), threading.Event(), threading.Event()
    p.before_send = lambda: (entered.set(), release.wait(0.8))
    worker = threading.Thread(target=lambda: c.admit_music_goal(req))
    worker.start()
    assert entered.wait(1)

    def remove():
        c.db.remove_from_playlist(pid, mid)
        removed.set()

    remover = threading.Thread(target=remove)
    remover.start()
    assert not removed.wait(0.03)  # authority lock stays owned until commit finishes
    release.set()
    worker.join(1)
    remover.join(1)
    assert not worker.is_alive() and not remover.is_alive() and removed.is_set()
    assert in_worker(lambda: c.observe_music_goal(req.operation_id)).status is not S.STARTED


def test_closed_cancelled_and_reused_operation_id_never_reload(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    admit(c, req)
    assert admit(c, replace(req, plan="TRY_CHART")).status is S.REJECTED_STALE
    c.cancel_music_goal(req.operation_id)
    assert in_worker(lambda: c.observe_music_goal(req.operation_id)).status is S.REJECTED_STALE
    assert len(p.loads) == 1
    c.stop()
    assert admit(c, replace(req, operation_id="after-close")).status is S.REJECTED_STALE


def test_tokens_do_not_expose_local_paths_in_repr(setup):
    c, _, song, pid, mid = setup
    req = request(c, pid, mid)
    assert song["path"] not in repr(req)


def test_future_schema_refuses_read_and_does_not_repair_database(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    c.db._conn.execute("PRAGMA user_version=99")
    before = unchanged(c, p)
    result = admit(c, req)
    assert result.status is S.FAILED_NO_EFFECT and not result.safe_to_fallback
    assert c.db._conn.execute("PRAGMA user_version").fetchone()[0] == 99
    assert unchanged(c, p) == before


def test_guarded_write_error_rolls_back_only_its_own_uncommitted_transaction(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    original = c.db.increment_play_count

    def broken(number):
        c.db._conn.execute("UPDATE music SET play_count=777 WHERE id=?", (number,))
        raise OSError("generated write failure")

    c.db.increment_play_count = broken
    value = admit(c, req)
    assert value.status is S.UNKNOWN_EFFECTS and value.effects_started
    assert not c.db._conn.in_transaction
    assert c.db._conn.execute("SELECT play_count FROM music WHERE id=1").fetchone()[0] != 777
    c.db.increment_play_count = original


def test_source_closed_and_media_permission_unknown_fail_closed(setup):
    c, p, _, pid, mid = setup
    c.online_manager.close()
    req = request(c, pid, mid, plan="TRY_CHART")
    assert admit(c, req).reason == "source_owner_unavailable"
    c.state_store.set("movie", "playing", None)
    req = request(c, pid, mid)
    before = unchanged(c, p)
    assert admit(c, req).status is S.REJECTED_UNAVAILABLE
    assert unchanged(c, p) == before


def test_uncommitted_owner_transaction_is_preserved_when_lease_rejects(setup):
    c, p, _, pid, mid = setup
    req = request(c, pid, mid)
    c.db._conn.execute("BEGIN IMMEDIATE")
    try:
        result = admit(c, req)
        assert result.status is S.FAILED_NO_EFFECT
        assert c.db._conn.in_transaction and not p.loads
    finally:
        c.db._conn.rollback()


def test_legacy_play_pause_resume_never_construct_the_opt_in_owner(setup):
    c, _, song, _, _ = setup
    assert not hasattr(c, "_goal_admission")
    c.play_song(song)
    c.pause()
    c.resume()
    assert not hasattr(c, "_goal_admission")


def test_noop_does_not_require_permission_to_change_other_media(setup):
    c, p, song, pid, mid = setup
    c.play_song(song)
    c.state_store.set("movie", "playing", True)
    req = request(c, pid, mid, plan="NO_OP")
    before = unchanged(c, p)
    assert admit(c, req).status is S.ACCEPTED
    in_worker(lambda: c.observe_music_goal(req.operation_id))
    assert in_worker(lambda: c.observe_music_goal(req.operation_id)).status is S.STARTED
    assert unchanged(c, p) == before
