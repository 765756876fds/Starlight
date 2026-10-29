"""Controller-level tests for playlist voice commands. Uses fakes, no real mpv."""
import pytest
from unittest.mock import MagicMock, patch
from app.music.controller import MusicController
from app.core.command_bus import Command


def _make_controller():
    c = MusicController.__new__(MusicController)
    c.queue = MagicMock()
    c.db = MagicMock()
    c.online_manager = None
    c._mpv_available = False
    c._voice_rank_tracks = []
    return c


def test_add_next_success():
    c = _make_controller()
    c.queue.get_current_index.return_value = 0
    c.queue.add.return_value = None
    song = {"title": "测试"}
    assert c.add_next(song) is True
    c.queue.add.assert_called_once_with(song, position=1)


def test_add_next_failure():
    c = _make_controller()
    c.queue.get_current_index.side_effect = RuntimeError("boom")
    assert c.add_next({"title": "x"}) is False


def test_play_playlist_not_found():
    c = _make_controller()
    c.get_playlists = MagicMock(return_value=[])
    cmd = Command(command_type="music.play_playlist", params={"name": "不存在"})
    assert c._cmd_play_playlist(cmd) is False


def test_play_playlist_duplicate_name():
    c = _make_controller()
    c.get_playlists = MagicMock(return_value=[{"id": 1, "name": "工作"}, {"id": 2, "name": "工作"}])
    cmd = Command(command_type="music.play_playlist", params={"name": "工作"})
    assert c._cmd_play_playlist(cmd) is False


def test_play_rank_index_out_of_range():
    c = _make_controller()
    c._voice_rank_tracks = []
    cmd = Command(command_type="music.play_rank_index", params={"index": 5})
    assert c._cmd_play_rank_index(cmd) is False


def test_create_playlist_empty_name():
    c = _make_controller()
    cmd = Command(command_type="music.create_playlist", params={"name": "  "})
    assert c._cmd_create_playlist(cmd) is False


def test_add_next_no_query():
    c = _make_controller()
    cmd = Command(command_type="music.add_next", params={})
    assert c._cmd_add_next(cmd) is False


def test_play_playlist_enqueues_in_order():
    """3 首歌单：播第一首 + 其余按原顺序入队（逆序调用 add_next 保持顺序）。"""
    c = _make_controller()
    c.get_playlists = MagicMock(return_value=[{"id": 1, "name": "工作"}])
    songs = [
        {"music_id": 1, "title": "A"},
        {"music_id": 2, "title": "B"},
        {"music_id": 3, "title": "C"},
    ]
    c.get_playlist_songs = MagicMock(return_value=songs)
    c.play_playlist_item = MagicMock(return_value=True)
    c.queue.get_current_index.return_value = 0
    c.queue.add.return_value = None
    cmd = Command(command_type="music.play_playlist", params={"name": "工作"})
    assert c._cmd_play_playlist(cmd) is True
    # add_next 被调用两次（B、C），逆序调用所以先 C 后 B
    calls = [c.args[0]["title"] for c in c.queue.add.call_args_list]
    # position=1 for both, but songs inserted: first C then B → queue order A, B, C
    assert calls == ["C", "B"], f"逆序入队顺序错误: {calls}"


def test_add_next_queue_add_raises():
    c = _make_controller()
    c.queue.get_current_index.return_value = 0
    c.queue.add.side_effect = RuntimeError("queue full")
    assert c.add_next({"title": "x"}) is False


def test_cmd_add_next_propagates_failure():
    c = _make_controller()
    c.online_manager = MagicMock()
    c.online_manager.search.return_value = [{"title": "七里香"}]
    c.queue.get_current_index.return_value = 0
    c.queue.add.side_effect = RuntimeError("boom")
    cmd = Command(command_type="music.add_next", params={"query": "七里香"})
    assert c._cmd_add_next(cmd) is False


def test_cmd_play_playlist_fails_on_enqueue_error():
    c = _make_controller()
    c.get_playlists = MagicMock(return_value=[{"id": 1, "name": "工作"}])
    songs = [{"music_id": 1, "title": "A"}, {"music_id": 2, "title": "B"}]
    c.get_playlist_songs = MagicMock(return_value=songs)
    c.play_playlist_item = MagicMock(return_value=True)
    c.queue.get_current_index.return_value = 0
    c.queue.add.side_effect = RuntimeError("queue full")
    cmd = Command(command_type="music.play_playlist", params={"name": "工作"})
    assert c._cmd_play_playlist(cmd) is False
