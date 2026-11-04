"""Next-play requests are consumed through the real queue/controller paths."""
import threading
from unittest.mock import Mock, patch

import pytest

from app.music.controller import MusicController
from app.music.queue import PlayMode, PlayQueue


MODES = [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ALL, PlayMode.REPEAT_ONE]


@pytest.fixture(autouse=True)
def deterministic_shuffle(monkeypatch):
    # Fixed random outcome, not a replacement for the queue selection algorithm.
    monkeypatch.setattr("app.music.queue.random.shuffle", lambda values: None)


def queue(mode=PlayMode.SHUFFLE):
    q = PlayQueue()
    q.add_all([{"title": title} for title in "ABC"])
    q.set_play_mode(mode)
    assert q.next()["title"] == "A"
    return q


def controller(mode):
    c = MusicController.__new__(MusicController)
    c.queue = queue(mode)
    c._lock = threading.Lock()
    c._tl = threading.local()
    c._trace = None
    c._mpv_available = True
    c.lx_connected = False
    c._last_next_ts = 0.
    c._running = True
    c._user_stopped = False
    c._candidate_switching = False
    c._last_idle = False
    c.mpv = Mock()
    c.mpv.get_property.return_value = True  # EOF observed by _auto_next.
    c._cancel_candidate_switch = Mock()
    c._invalidate_highlight = Mock()
    c._record_decision = Mock()
    c._play_song = Mock()
    return c


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("automatic", [False, True])
def test_requested_next_is_dispatched_by_manual_and_eof_paths(mode, automatic):
    c = controller(mode)
    requested = {"title": "D"}
    assert c.add_next(requested) is True
    assert c.queue.get_current()["title"] == "A"
    (c._auto_next if automatic else c.next)()
    c._play_song.assert_called_once_with(requested)
    assert c.queue.get_current() is requested
    assert c.queue.get_play_mode() == mode


def test_shuffle_resumes_unplayed_order_without_repeating_the_request():
    q = queue()
    q.add_next({"title": "D"})
    assert [q.next(auto=True)["title"] for _ in range(3)] == ["D", "B", "C"]
    # Future shuffle cycles still include every song, as before.
    assert [q.next()["title"] for _ in range(4)] == ["A", "D", "B", "C"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("automatic", [False, True])
def test_multiple_next_requests_are_last_selected_first(mode, automatic):
    q = queue(mode)
    q.add_next({"title": "D"})
    q.add_next({"title": "E"})
    assert [s["title"] for s in q.get_queue()] == list("AEDBC")
    assert [q.next(auto=automatic)["title"] for _ in range(2)] == ["E", "D"]
    expected = "D" if mode == PlayMode.REPEAT_ONE and automatic else "B"
    assert q.next(auto=automatic)["title"] == expected


def test_insertion_keeps_previous_song_history_pointing_to_the_same_songs():
    q = queue()
    q.play_at(2)  # C
    q.play_at(0)  # A
    q.add_next({"title": "D"})
    assert q.next()["title"] == "D"
    assert q.prev()["title"] == "A"
    assert q.prev()["title"] == "C"
    assert q.next()["title"] == "B"  # Original remaining random order.


@pytest.mark.parametrize("mode", MODES)
def test_mode_changes_keep_pending_requests_and_queue_contents(mode):
    q = queue(mode)
    q.add_next({"title": "D"})
    q.add_next({"title": "E"})
    for new_mode in MODES:
        q.set_play_mode(new_mode)
        assert [s["title"] for s in q.get_queue()] == list("AEDBC")
        assert q.get_current()["title"] == "A"
    assert q.next(auto=True)["title"] == "E"
    assert q.next(auto=True)["title"] == "D"
    assert q.next(auto=True)["title"] == "D"  # Single repeat resumes on D.


def test_preview_does_not_consume_pending_request():
    q = queue()
    q.add_next({"title": "D"})
    assert q.get_next()["title"] == q.get_next()["title"] == "D"
    assert q.get_current()["title"] == "A"
    assert q.next()["title"] == "D"
    assert q.next()["title"] == "B"


def test_clear_discards_pending_request_before_replacing_queue():
    q = queue()
    q.add_next({"title": "D"})
    q.clear()
    q.add_all([{"title": "X"}, {"title": "Y"}])
    assert [q.next()["title"] for _ in range(2)] == ["X", "Y"]


def test_removing_pending_item_and_earlier_item_remaps_pending_request():
    q = queue()
    q.add_next({"title": "D"})
    q.add_next({"title": "E"})
    q.remove(1)  # E
    q.remove(0)  # A
    assert q.next()["title"] == "D"
    assert q.next()["title"] == "B"


def test_general_insertion_and_append_do_not_lose_pending_request():
    q = queue()
    q.add_next({"title": "D"})
    q.add({"title": "X"}, position=0)
    q.add_all([{"title": "Y"}])
    assert q.get_current()["title"] == "A"
    assert q.next()["title"] == "D"
    assert [q.next()["title"] for _ in range(5)] == list("XABCY")


def test_playing_requested_item_directly_does_not_request_it_again():
    q = queue()
    q.add_next({"title": "D"})
    assert q.play_at(1)["title"] == "D"
    assert q.next()["title"] == "B"


def test_bad_single_repeat_request_escapes_to_remaining_next_request():
    q = queue(PlayMode.REPEAT_ONE)
    q.add_next({"title": "D"})
    q.add_next({"title": "E"})
    assert q.next(auto=True)["title"] == "E"
    # Existing missing-file escape selects the remaining request, not a random song.
    assert q.skip_current()["title"] == "D"
    assert q.next(auto=True)["title"] == "D"
    q.set_play_mode(PlayMode.SHUFFLE)
    assert q.next()["title"] == "A"


def test_two_explicit_requests_for_same_song_are_two_distinct_queue_entries():
    q = queue()
    song = {"title": "D"}
    q.add_next(song)
    q.add_next(song)
    assert q.next() is song
    assert q.next() is song
    assert q.next()["title"] == "B"
    assert q.size() == 5


@pytest.mark.parametrize("mode", MODES)
def test_first_request_without_current_media_is_playable(mode):
    q = PlayQueue()
    q.set_play_mode(mode)
    q.add_next({"title": "D"})
    assert q.next(auto=True)["title"] == "D"
    assert q.size() == 1
    if mode != PlayMode.SEQUENTIAL:
        assert q.next(auto=True)["title"] == "D"


@pytest.fixture
def playback(tmp_path, monkeypatch):
    with patch("app.music.controller.MpvController"), patch("app.music.controller.LxMusicClient") as lx:
        lx.return_value.is_connected.return_value = False
        c = MusicController({"database": str(tmp_path / "music.db"), "scan_dirs": [],
                             "mpv_path": "mpv", "default_volume": 70})
    c.mpv.loadfile.return_value = {"error": "success"}
    c.mpv._properties = {}
    c.set_movie_pauser(Mock(return_value=True))
    c._mpv_available = True
    c._running = True
    monkeypatch.setattr(c, "_confirm_playback", Mock())
    songs = []
    for title in "ACD":
        path = tmp_path / (title + ".mp3")
        path.write_bytes(b"isolated fake media; no audio decoding in this test")
        song = {"title": title, "artist": "test", "path": str(path), "filename": path.name}
        song["id"] = c.db.insert_song(song)
        songs.append(song)
    c.queue.add_all([songs[0], {"title": "B", "source_type": "online"}, songs[1]])
    c.queue.set_play_mode(PlayMode.SHUFFLE)
    with c._lock:
        c._play_song(c.queue.next())
    c.mpv.loadfile.reset_mock()
    yield c, songs[2]
    c.stop()
    c.db.close()


@pytest.mark.parametrize("kind", ["local", "online"])
def test_inserted_local_and_online_songs_reach_existing_playback_paths(playback, kind, monkeypatch):
    c, local_song = playback
    if kind == "local":
        assert c.add_next(local_song) is True
        c.next()
        c.mpv.loadfile.assert_called_once_with(local_song["path"])
    else:
        done = threading.Event()
        worker = c._resolve_online_worker

        def observed_worker(*args):
            try:
                return worker(*args)
            finally:
                done.set()

        monkeypatch.setattr(c, "_resolve_online_worker", observed_worker)
        c._resolver = Mock()
        c.online_manager = Mock()
        c.online_manager.get_play_url.return_value = {"url": "https://example.test/D.mp3"}
        song = {"title": "D", "artist": "test", "source_type": "online",
                "source_name": "fake", "track_id": "D", "extra": {"_raw": {"id": "D"}}}
        assert c.add_next(song) is True
        c.next()
        assert done.wait(2.), "online queue worker did not finish"
        c.mpv.loadfile.assert_called_once_with("https://example.test/D.mp3")
        c._resolver.resolve.assert_not_called()
    assert c.get_current_song()["title"] == "D"
    assert c.queue.get_current()["title"] == "D"
    assert c.queue.size() == 4
