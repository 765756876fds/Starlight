"""Queue display queries must not select a track or touch playback IPC."""
import copy
import random
import threading
from unittest.mock import Mock, patch

import pytest

from app.core.event_bus import EventType
from app.core.state_store import StateNamespace, StateStore
from app.music.controller import MusicController
from app.music.queue import PlayMode, PlayQueue


MODES = [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ALL, PlayMode.REPEAT_ONE]


def song(title):
    return {"title": title, "artist": "歌手", "path": f"{title}.mp3"}


def queue_state(q):
    return copy.deepcopy((q._queue, q._current_index, q._history, q._history_pos,
                          q._shuffle_order, q._shuffle_pos, q._next_up, q._play_mode))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("pending", [False, True])
def test_repeated_snapshots_leave_all_playback_and_random_state_unchanged(mode, pending):
    q = PlayQueue()
    q.add_all([song(t) for t in "ABC"])
    q.set_play_mode(mode)
    q.play_at(0)
    if pending:
        q.add_next(song("D"))
        q.add_next(song("E"))
    before, rng = queue_state(q), random.getstate()
    with patch.object(q, "get_next", side_effect=AssertionError("selector called")), \
         patch.object(q, "_get_next_index", side_effect=AssertionError("selector called")):
        for _ in range(10):
            snapshot = q.snapshot(q.get_current())
    assert queue_state(q) == before
    assert random.getstate() == rng
    assert snapshot["playing_index"] == 0
    assert [snapshot["items"][i]["title"] for i in snapshot["next_up"]] == (list("ED") if pending else [])
    if pending:
        assert [q.next(auto=True)["title"] for _ in range(2)] == list("ED")


def test_snapshot_copies_only_display_metadata():
    q = PlayQueue()
    q.add({"title": "在线歌", "artist": "歌手", "source_type": "online",
           "source_name": "音源", "track_id": "1", "extra": {"headers": {"secret": "value"}}})
    snapshot = q.snapshot()
    assert set(snapshot["items"][0]) == {"title", "artist", "source_type", "source_name", "index"}
    snapshot["items"][0]["title"] = "changed"
    snapshot["next_up"].append(42)
    assert q.snapshot()["items"][0]["title"] == "在线歌"
    assert q.snapshot()["next_up"] == []


def test_online_current_matches_identity_not_dictionary_layout_or_title():
    q = PlayQueue()
    q.add({"title": "同名歌", "source_type": "online", "source_name": "源A", "track_id": "7"})
    q.play_at(0)
    actual = {"title": "同名歌", "source": "online", "source_name": "源A", "track_id": "7",
              "path": "https://example.invalid/audio"}
    assert q.snapshot(actual)["playing_index"] == 0
    assert q.snapshot(dict(actual, track_id="8"))["playing_index"] is None
    assert q.snapshot(dict(actual, source_name="源B"))["playing_index"] is None


def test_external_current_does_not_falsely_mark_queue_cursor_as_playing():
    q = PlayQueue()
    q.add(song("A"))
    q.play_at(0)
    assert q.snapshot(song("X"))["playing_index"] is None
    assert q.snapshot(None)["playing_index"] is None
    assert q.snapshot(dict(song("A")))["playing_index"] == 0


@pytest.mark.parametrize("value", [None, {}, {"title": None, "artist": 5}, {"title": "<b>A</b>"}])
def test_display_metadata_is_safe_for_missing_or_unusual_text(value):
    summary = PlayQueue.song_summary(value)
    assert isinstance(summary["title"], str) and summary["title"]
    assert isinstance(summary["artist"], str)


def test_notifications_cover_mutations_and_happen_after_queue_unlock():
    q = PlayQueue()
    snapshots = []

    def changed():
        assert q._lock.acquire(False), "notification holds queue lock"
        q._lock.release()
        snapshots.append(q.snapshot())

    q.set_change_callback(changed)
    actions = [lambda: q.add(song("A")), lambda: q.add_all([song("B")]),
               lambda: q.play_at(0), lambda: q.add_next(song("D")), lambda: q.next(),
               lambda: q.prev(), lambda: q.skip_current(), lambda: q.set_play_mode("shuffle"),
               lambda: q.remove(0), q.clear]
    for count, action in enumerate(actions, 1):
        action()
        assert len(snapshots) == count
    assert snapshots[-1]["items"] == []


def test_failed_display_notification_cannot_break_playback():
    q = PlayQueue()
    q.set_change_callback(Mock(side_effect=RuntimeError("view gone")))
    q.add(song("A"))
    assert q.next()["title"] == "A"


def controller():
    c = MusicController.__new__(MusicController)
    c._lock = threading.Lock()
    c.queue = PlayQueue()
    c._current_song = song("A")
    c._is_switching_track = False
    c._paused_snapshot = False
    c._running = True
    c.state_store = StateStore()
    c.mpv = Mock()
    c.event_bus = Mock()
    c.queue.add(c._current_song)
    c.queue.play_at(0)
    return c


@pytest.mark.parametrize("playing,paused,switching,running,expected", [
    (True, False, False, True, "playing"), (False, True, False, True, "paused"),
    (False, False, False, True, "stopped"), (True, False, True, True, "loading"),
    (True, False, False, False, "stopped"), (None, False, False, True, "unknown"),
    (True, True, False, True, "playing"),  # direct online start has a newer confirmed playing state
])
def test_controller_snapshot_uses_memory_status_and_never_mpv(playing, paused, switching, running, expected):
    c = controller()
    c.state_store.set(StateNamespace.MUSIC, "playing", playing)
    c._paused_snapshot, c._is_switching_track, c._running = paused, switching, running
    assert c.get_queue_snapshot()["state"] == expected
    assert c.mpv.mock_calls == []


def test_controller_busy_query_returns_without_waiting_on_playback_lock():
    c = controller()
    with c._lock:
        assert c.get_queue_snapshot() is None
    assert c.get_queue_snapshot()["current"]["title"] == "A"


def test_state_updates_invalidate_view_without_overwriting_sleep_timer_event_key():
    c = controller()
    c._update_state(playing=True)
    event = c.event_bus.publish_async.call_args.args[0]
    assert event.event_type == EventType.MUSIC_STATE_CHANGED
    assert event.data == {"state": "queue_changed", "entity_key": "music.queue_view",
                          "timestamp": event.timestamp}


def test_real_controller_wires_queue_notifications_once(tmp_path):
    with patch("app.music.controller.MpvController"), patch("app.music.controller.LxMusicClient"):
        c = MusicController({"database": str(tmp_path / "music.db"), "scan_dirs": []})
    c.event_bus = Mock()
    try:
        c.queue.add(song("A"))
        c.queue.add_next(song("D"))
        assert c.event_bus.publish_async.call_count == 2
        assert c.get_queue_snapshot()["next_up"] == [0]
    finally:
        c.db.close()
