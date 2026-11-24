import threading

from app.music.online.models import OnlineTrack
from app.music.queue import PlayMode, PlayQueue
from tests.unit.test_music_recommendation import controller as controller


def test_context_replaces_only_implicit_entries_and_keeps_occurrence_ids():
    queue = PlayQueue()
    duplicate = {"title": "重复"}
    queued = {"title": "追加"}
    queue.add({"title": "旧榜单"})
    queue.play_at(0)
    queue.add_next({"title": "先指定"})
    queue.add_next({"title": "后指定"})
    queue.add(queued, explicit=True)
    before = {row["title"]: row["entry_id"] for row in queue.snapshot()["items"]}
    queue.replace_context([duplicate, duplicate, {"title": "列表尾"}], 1)
    after = queue.snapshot(duplicate)
    assert after["playing_index"] == 1
    titles = [row["title"] for row in after["items"]]
    assert titles == ["重复", "重复", "后指定", "先指定", "列表尾", "追加"]
    assert after["items"][0]["entry_id"] != after["items"][1]["entry_id"]
    for row in after["items"][2:]:
        if row["title"] in before:
            assert row["entry_id"] == before[row["title"]]
    assert [queue.next()["title"] for _ in range(4)] == ["后指定", "先指定", "列表尾", "追加"]
    queue.replace_context([duplicate], 0)
    assert queue.get_queue() == [duplicate]


def test_context_shuffle_excludes_current_occurrence_and_repeat_one_reuses_it():
    queue = PlayQueue()
    repeated = {"title": "重复"}
    queue.set_play_mode(PlayMode.SHUFFLE)
    queue.replace_context([repeated, repeated, {"title": "不同"}], 1)
    played = []
    for _ in range(2):
        queue.next(auto=True)
        played.append(queue.get_current_index())
    assert set(played) == {0, 2}
    queue.set_play_mode(PlayMode.REPEAT_ONE)
    queue.replace_context([repeated, repeated], 1)
    assert queue.next(auto=True) is repeated and queue.get_current_index() == 1
    queue.add_next({"title": "临时"})
    assert queue.next(auto=True)["title"] == "临时"


def test_rank_to_rank_search_and_failed_selection_use_only_the_latest_context(controller):
    c = controller
    first, second = c.online_manager.tracks[:3], c.online_manager.tracks[3:6]
    assert c.play_online_list(first, 1)
    assert c.play_online_list(second, 2)
    assert [row["track_id"] for row in c.get_queue()] == ["3", "4", "5"]
    assert c.queue.snapshot(c.get_current_song())["playing_index"] == 2
    c.online_manager.fail.add("0")
    assert not c.play_online_list(first, 0)
    assert [row["track_id"] for row in c.get_queue()] == ["0", "1", "2"]
    assert c.get_current_song()["track_id"] == "5"  # No successful load was fabricated.
    assert c.play_online(second[0], _single_context=True)
    assert [row["track_id"] for row in c.get_queue()] == ["3"]
    assert c.queue.next(auto=True) is None


def test_playlist_local_library_and_back_keep_visible_order(controller, tmp_path, monkeypatch):
    c = controller
    monkeypatch.setattr(c, "_confirm_playback", lambda *_: None)
    local = []
    for title in ("B", "C"):
        path = tmp_path / f"{title}.wav"
        path.touch()
        row = dict(title=title, artist="测试", path=str(path), filename=path.name, format="wav")
        row["id"] = c.db.insert_song(row)
        local.append(row)
    saved = [dict(source_type="local", music_id=local[1]["id"]),
             dict(source_type="online", title="网曲", source_name="fixture", track_id="p"),
             dict(source_type="local", music_id=local[0]["id"])]
    c.play_playlist_context(saved, 0)
    assert [row["title"] for row in c.get_queue()] == ["C", "网曲", "B"]
    c.play_context(local, 0)
    assert [row["title"] for row in c.get_queue()] == ["B", "C"]
    assert c.get_current_song()["title"] == "B"
    assert c.play_playlist_context(saved, 1)
    assert [row["title"] for row in c.get_queue()] == ["C", "网曲", "B"]
    assert c.queue.snapshot(c.get_current_song())["playing_index"] == 1


def test_delayed_address_and_late_worker_cannot_replace_the_new_list(controller, monkeypatch):
    c = controller
    start, release = threading.Event(), threading.Event()
    first, second = c.online_manager.tracks[:2], c.online_manager.tracks[2:4]
    regular = c.online_manager.get_play_url

    def address(track, **options):
        if track is first[0]:
            start.set()
            assert release.wait(4)
        return regular(track, **options)

    monkeypatch.setattr(c.online_manager, "get_play_url", address)
    result = []
    worker = threading.Thread(target=lambda: result.append(c.play_online_list(first)))
    worker.start()
    try:
        assert start.wait(2)
        assert c.play_online_list(second)
    finally:
        release.set()
        worker.join(4)
    assert result == [False]
    assert not c.play_online_list(first, _before_load=lambda: False)
    assert c.mpv.loaded == ["https://fixture.invalid/2"]
    assert [row["track_id"] for row in c.get_queue()] == ["2", "3"]


def test_equivalent_source_fallback_confirms_the_queue_occurrence(controller):
    c = controller
    first = OnlineTrack("fixture", "a", "同一首歌", "某歌手", "同一张专辑", 180)
    other = OnlineTrack("backup", "b", "同一首歌", "某歌手", "同一张专辑", 180)
    c.online_manager.fail.add("a")
    assert c.play_online(first, _single_context=True, _alternatives=(other,))
    actual = c.get_current_song()
    assert actual["source_name"] == "backup" and actual["track_id"] == "b"
    assert c.queue.snapshot(actual)["playing_index"] == 0
