from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.music.queue import PlayQueue, PlayMode
from app.core.operation_result import OperationStatus
from tests.unit.test_music_queue_snapshot import controller


def build(mode="sequential"):
    queue = PlayQueue()
    songs = [{"title": name, "path": name + ".mp3"} for name in "ABCDE"]
    queue.add_all(songs)
    queue.play_at(0)
    queue.set_play_mode(mode)
    return queue, songs


@pytest.mark.parametrize("mode", ["sequential", "repeat_all", "repeat_one"])
def test_reordering_waiting_entries_preserves_current_and_actual_next(mode):
    queue, songs = build(mode)
    state = queue.snapshot(songs[0])
    assert queue.move_waiting(state["items"][3]["entry_id"], state["items"][1]["entry_id"], state["revision"], songs[0])
    assert queue.get_current() is songs[0]
    assert queue.next(auto=False) is songs[3]


def test_shuffle_move_rejected_without_touching_cursor_order_or_history():
    queue, songs = build(PlayMode.SHUFFLE)
    state = queue.snapshot(songs[0])
    before = deepcopy((queue._history, queue._history_pos, queue._shuffle_order, queue._shuffle_pos))
    assert queue.move_waiting(state["items"][3]["entry_id"], None, state["revision"], songs[0]) is False
    assert before == (queue._history, queue._history_pos, queue._shuffle_order, queue._shuffle_pos)
    assert state == queue.snapshot(songs[0])


def test_priority_e_then_d_stays_ahead_of_reordered_normal_entries():
    queue, songs = build()
    queue.add_next({"title": "插队D", "path": "priority-d"})
    queue.add_next({"title": "插队E", "path": "priority-e"})
    state = queue.snapshot(songs[0])
    assert queue.move_waiting(state["items"][-1]["entry_id"], state["items"][3]["entry_id"], state["revision"], songs[0])
    assert queue.next(auto=True)["title"] == "插队E"
    assert queue.next(auto=True)["title"] == "插队D"
    assert queue.next(auto=True) is songs[4]


def test_duplicate_occurrences_have_distinct_stable_ids_and_exact_move():
    queue, songs = build()
    queue.add(songs[1])
    state = queue.snapshot(songs[0])
    first_id, duplicate_id = state["items"][1]["entry_id"], state["items"][-1]["entry_id"]
    assert first_id != duplicate_id
    assert queue.move_waiting(duplicate_id, first_id, state["revision"], songs[0])
    after = queue.snapshot(songs[0])
    assert after["items"][1]["entry_id"] == duplicate_id
    assert after["items"][2]["entry_id"] == first_id
    assert queue.next() is songs[1]
    assert queue.snapshot(songs[1])["items"][queue.get_current_index()]["entry_id"] == duplicate_id


@pytest.mark.parametrize("target", ["current", "priority", "missing", "stale"])
def test_invalid_reorder_does_not_change_queue(target):
    queue, songs = build()
    queue.add_next({"title": "优先", "path": "priority"})
    state = queue.snapshot(songs[0])
    chosen = state["items"][0 if target == "current" else 1 if target == "priority" else 3]["entry_id"]
    assert queue.move_waiting("not-an-entry" if target == "missing" else chosen, None,
                              state["revision"] - (target == "stale"), songs[0]) is False
    assert state == queue.snapshot(songs[0])


def test_ids_survive_insert_remove_and_are_not_reused_after_clear():
    queue, songs = build()
    original = queue.snapshot()["items"][2]["entry_id"]
    queue.add_next({"title": "额外", "path": "extra"})
    assert queue.snapshot()["items"][3]["entry_id"] == original
    queue.remove(1)
    assert queue.snapshot()["items"][2]["entry_id"] == original
    queue.clear()
    queue.add(songs[2])
    assert queue.snapshot()["items"][0]["entry_id"] != original


def test_controller_reorder_uses_no_player_or_database_calls():
    c = controller()
    c.queue.add_all([{"title": "B", "path": "B"}, {"title": "C", "path": "C"}])
    state = c.get_queue_snapshot()
    before = c._current_song
    result = c.move_queue_entry(state["items"][2]["entry_id"], state["items"][1]["entry_id"], state["revision"])
    assert result.status is OperationStatus.CONFIRMED
    assert c._current_song is before and not c.mpv.mock_calls


def test_controller_busy_and_shuffle_have_accurate_unavailable_results():
    c = controller()
    state = c.get_queue_snapshot()
    with c._lock:
        assert c.move_queue_entry("entry", None, state["revision"]).status is OperationStatus.UNAVAILABLE
    c.queue.set_play_mode("shuffle")
    assert c.move_queue_entry("entry", None, state["revision"]).status is OperationStatus.UNAVAILABLE
