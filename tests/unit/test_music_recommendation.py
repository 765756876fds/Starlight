"""Local memory, actual command/controller dispatch and bounded fake-source failures."""
import random
import threading
from unittest.mock import Mock, patch

import pytest

from app.core.command_bus import Command, CommandBus, CommandType
from app.core.operation_result import OperationStatus
from app.music.controller import MusicController
from app.music.online.models import OnlineTrack
from app.music.preferences import PreferenceStore, media_key
from app.music.recommendation import track_row
from app.voice.intent import IntentRouter


def song(number=0, artist="甲"):
    return OnlineTrack("fixture", str(number), f"歌曲{number}", artist, duration_seconds=180)


class Sources:
    def __init__(self):
        self.details = 0
        self.urls = []
        self.fail = set()
        self.tracks = [song(n, "乙" if n == 1 else "甲") for n in range(10)]

    def get_top_lists(self):
        return {"fixture": [{"title": "热门", "lists": [
            {"id": "hot", "title": "轻松热门榜"}, {"id": "sad", "title": "伤感抒情榜"}]}]}

    def get_top_list_detail(self, source, list_id, page=1):
        self.details += 1
        return {"tracks": self.tracks, "is_end": True}

    def get_play_url(self, track, **kwargs):
        self.urls.append(track.track_id)
        if track.track_id in self.fail:
            raise TimeoutError("fixture source timeout")
        return {"url": "https://fixture.invalid/" + track.track_id}

    def close(self):
        pass


class Player:
    def __init__(self):
        self.loaded = []
        self._properties = {}
        self.response = {"error": "success"}

    def loadfile(self, url):
        self.loaded.append(url)
        return self.response

    def set_property(self, key, value):
        self._properties[key] = value

    def get_property(self, key, **kwargs):
        if key == "path":
            return self.loaded[-1] if self.loaded else None
        if key == "idle-active":
            return False
        return 0 if key == "time-pos" else self._properties.get(key)

    def on_property_change(self, *args):
        pass

    def stop_process(self):
        pass


@pytest.fixture
def controller(tmp_path, monkeypatch):
    from app.core.event_bus import EventBus
    from app.core.state_store import StateStore
    bus = CommandBus()
    monkeypatch.setattr("app.music.controller.get_command_bus", lambda: bus)
    monkeypatch.setattr("app.music.controller.get_state_store", StateStore)
    monkeypatch.setattr("app.music.controller.get_event_bus", EventBus)
    with patch("app.music.controller.LxMusicClient") as lx:
        lx.return_value.is_connected.return_value = False
        c = MusicController({"database": str(tmp_path / "music.db"), "scan_dirs": []}, mpv_adapter=Player())
    c.set_movie_pauser(lambda: True)
    c.online_manager = Sources()
    c.remember_cover = Mock()
    c.recommendations.rng = random.Random(4)
    yield c
    c.stop()
    c.db.close()


def test_memory_reopens_and_keeps_explicit_separate(tmp_path):
    path = tmp_path / "preferences.db"
    memory = PreferenceStore(path)
    memory.record(track_row(song()), "manual")
    memory.set_preference("artist", "乙", 1)
    reopened = PreferenceStore(path)
    assert reopened.profile()["explicit"][("artist", "乙")] == 1
    assert reopened.profile()["artists"] == {"甲": 1}


def test_auto_play_does_not_reinforce_and_legacy_seed_is_once(tmp_path):
    memory = PreferenceStore(tmp_path / "memory.db")
    for _ in range(20):
        memory.record(track_row(song()), "recommendation")
        memory.record(track_row(song()), "automatic")
    assert memory.profile()["artists"] == {}
    memory.import_history_once([track_row(song())])
    memory.import_history_once([track_row(song(1, "乙"))])
    assert memory.profile()["artists"] == {"甲": .15}


@pytest.mark.parametrize("bad", ["corrupt", "directory", "parent-file"])
def test_memory_failure_degrades_without_destroying_files(tmp_path, bad):
    path = tmp_path / "memory.db"
    if bad == "corrupt":
        path.write_bytes(b"preserve original corrupt evidence")
    elif bad == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"not a directory")
        path = path / "child.db"
    memory = PreferenceStore(path)
    assert memory.profile() == {"explicit": {}, "artists": {}}
    assert memory.available is False
    assert memory.set_preference("artist", "甲", 1) is False


def test_identity_never_merges_versions_or_sources():
    a = track_row(song())
    assert media_key(a) != media_key(dict(a, source_name="other"))
    assert media_key(a) != media_key(dict(a, title=a["title"] + "（现场）"))
    assert media_key(a) != media_key(dict(a, duration=12))


def test_cold_start_and_bounded_rank_randomness(controller):
    picks = set()
    for seed in range(12):
        controller.recommendations.rng = random.Random(seed)
        choices = controller.recommendations.choices(controller.online_manager)
        assert 0 < len(choices) <= 4
        picks.add(choices[0].index)
    assert len(picks) > 1


def test_explicit_artist_beats_heavy_inference(controller):
    for n in range(10):
        controller.music_memory.record(track_row(song(n, "甲")), "manual")
    controller.music_memory.set_preference("artist", "乙", 1)
    choices = controller.recommendations.choices(controller.online_manager)
    assert choices[0].tracks[choices[0].index].artist == "乙"
    controller.online_manager.fail = {"1"}
    controller.recommendations.rng = random.Random(4)
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    assert controller.online_manager.urls[0] == "1"
    assert len(controller.online_manager.urls) > 1  # Lower tier is fallback only after explicit favourite fails.


def test_explicit_style_and_negative_track(controller):
    controller.music_memory.set_preference("track", media_key(track_row(song(0))), -1)
    choices = controller.recommendations.choices(controller.online_manager, style="伤感")
    assert choices and all(c.chart["id"] == "sad" and c.index != 0 for c in choices)
    assert controller.recommendations.choices(controller.online_manager, style="不存在的风格") == []


def test_voice_to_bus_to_actual_controller_load_and_queue(controller):
    intent = IntentRouter({}).parse("我想听歌了")
    result = controller.command_bus.execute(Command(intent.intent_type, intent.params))
    assert result.status is OperationStatus.CONFIRMED
    assert len(controller.mpv.loaded) == 1
    assert controller.recommendations.session["title"] in result.message
    assert controller.music_memory.profile()["artists"] == {}
    reads = controller.online_manager.details
    queued = controller.queue.next()
    assert queued["source_name"] == "fixture"
    assert queued["extra"]["_starlight_recommended"] is True
    assert controller.online_manager.details == reads


def test_all_urls_fail_does_not_replace_user_queue(controller):
    controller.queue.add({"id": 42, "title": "原有手动歌曲"})
    controller.online_manager.fail = {str(n) for n in range(10)}
    result = controller.recommend_music()
    assert result.status is OperationStatus.FAILED
    assert controller.mpv.loaded == []
    assert controller.queue.next()["id"] == 42
    assert len(controller.online_manager.urls) == 4
    assert controller.recommendations.session is None


def test_first_source_candidate_fails_then_next_succeeds(controller):
    first = controller.recommendations.choices(controller.online_manager)[0]
    controller.recommendations.rng = random.Random(4)
    controller.online_manager.fail = {first.tracks[first.index].track_id}
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    assert len(controller.online_manager.urls) >= 2


def test_stale_voice_guard_does_not_load(controller):
    assert controller.recommend_music(is_current=lambda: False).status is OperationStatus.UNKNOWN
    assert controller.mpv.loaded == []


def test_manual_switch_during_catalog_does_not_get_overwritten(controller):
    read = controller.online_manager.get_top_lists
    def interleave():
        controller._play_request_id += 1
        return read()
    controller.online_manager.get_top_lists = interleave
    assert controller.recommend_music().status is OperationStatus.UNKNOWN
    assert controller.mpv.loaded == []


def test_ipc_failure_is_not_success_and_memory_remains_empty(controller):
    controller.mpv.response = {"error": "loading failed"}
    assert controller.recommend_music().status is OperationStatus.FAILED
    assert controller.recommendations.session is None
    assert controller.music_memory.profile()["artists"] == {}


def test_explicit_style_overrides_saved_style(controller):
    controller.music_memory.set_preference("style", "伤感", 1)
    choices = controller.recommendations.choices(controller.online_manager, style="轻松")
    assert choices and all(c.chart["id"] == "hot" for c in choices)


def test_change_chart_preserves_manual_playlists_and_history(controller):
    controller.db.create_playlist("保留歌单")
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    key = controller.recommendations.session["key"]
    assert controller.recommend_music(change_chart=True).status is OperationStatus.CONFIRMED
    assert controller.recommendations.session["key"] != key
    assert controller.get_playlists()[0]["name"] == "保留歌单"
    assert controller.db.get_history()


@pytest.mark.parametrize("text", ["我想听歌了", "放点歌听听", "放点我喜欢的", "来点伤感的", "换个榜单", "换个风格"])
def test_recommendation_voice(text):
    assert IntentRouter({}).parse(text).intent_type == CommandType.MUSIC_RECOMMEND


@pytest.mark.parametrize("text", ["不要放点歌听听", "怎么换个榜单", "他说我想听歌了", "我想听歌了？",
    "为什么换个风格", "来点伤感的是什么意思", "我想听歌了然后暂停音乐", "“我想听歌了”"])
def test_recommendation_voice_safety(text):
    assert IntentRouter({}).parse(text).intent_type in {"system.unknown", "system.clarify"}


def test_exact_song_and_rank_commands_unchanged():
    assert IntentRouter({}).parse("播放晴天").params == {"query": "晴天"}
    assert IntentRouter({}).parse("我想听周杰伦").params == {"query": "周杰伦"}


def test_next_without_recommendation_requires_target(controller):
    intent = IntentRouter({}).parse("换一个")
    result = controller.command_bus.execute(Command(intent.intent_type, intent.params))
    assert result.status is OperationStatus.NEEDS_PARAMETER
    assert controller.mpv.loaded == []


def test_one_pending_request_no_extra_threads(controller):
    entered, release = threading.Event(), threading.Event()
    original = controller.online_manager.get_top_lists
    def delayed():
        entered.set()
        assert release.wait(1)
        return original()
    controller.online_manager.get_top_lists = delayed
    controller.music_memory.import_history_once(controller.db.get_history(limit=100))
    controller.music_memory.profile()
    worker = threading.Thread(target=controller.recommend_music)
    worker.start()
    assert entered.wait(1)
    assert controller.recommend_music().status is OperationStatus.UNAVAILABLE
    controller.recommendations.cancel(close=True)
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert controller.mpv.loaded == []


def test_source_timeout_uses_existing_pool_and_next_request_recovers(controller):
    from concurrent.futures import ThreadPoolExecutor
    import time
    source = controller.online_manager
    source._executor = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    original = source.get_top_lists
    def slow():
        assert release.wait(1)
        return original()
    source.get_top_lists = slow
    controller.recommendations.READ_TIMEOUT = .03
    controller.music_memory.import_history_once([])
    controller.music_memory.profile()
    started = time.monotonic()
    try:
        result = controller.recommend_music()
        assert result.status is OperationStatus.FAILED
        assert time.monotonic() - started < .5
        assert not controller.mpv.loaded
        release.set()
        source.get_top_lists = original
        controller.recommendations.READ_TIMEOUT = 1
        assert controller.recommend_music().status is OperationStatus.CONFIRMED
    finally:
        release.set()
        source._executor.shutdown(wait=True, cancel_futures=True)


def test_broken_chart_does_not_block_other_chart(controller):
    read = controller.online_manager.get_top_list_detail
    def partial(source, list_id, **kwargs):
        if list_id == "hot":
            raise TimeoutError("fixture chart timed out")
        return read(source, list_id, **kwargs)
    controller.online_manager.get_top_list_detail = partial
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    assert controller.recommendations.session["key"][1] == "sad"


def test_recommendation_next_consumes_real_controller_path(controller):
    import time
    controller._resolver = object()
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    requests = controller.online_manager.details
    assert controller._cmd_recommend_next(None).status is OperationStatus.ACCEPTED
    deadline = time.monotonic() + 2
    while len(controller.mpv.loaded) < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(controller.mpv.loaded) == 2
    assert controller.online_manager.details == requests
    assert controller.music_memory.profile()["artists"] == {}


def test_manual_play_invalidates_recommendation_context(controller):
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    assert controller.play_online(song(50)) is True
    assert controller.recommendations.session is None
    assert controller._cmd_recommend_next(None).status is OperationStatus.NEEDS_PARAMETER
    assert controller.music_memory.profile()["artists"] == {"甲": 1}


def test_movie_playing_blocks_ambiguous_change(controller):
    from app.core.state_store import StateNamespace
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    controller.state_store.set(StateNamespace.MOVIE, "playing", True)
    try:
        assert controller._cmd_recommend_next(None).status is OperationStatus.NEEDS_PARAMETER
        assert len(controller.mpv.loaded) == 1
    finally:
        controller.state_store.set(StateNamespace.MOVIE, "playing", False)


def test_language_and_decade_require_real_metadata(controller):
    tracks = controller.online_manager.tracks
    tracks[2].extra = {"language": "英语", "year": 1995}
    controller.music_memory.set_preference("language", "英语", 1)
    controller.music_memory.set_preference("era", "1990", 1)
    choices = controller.recommendations.choices(controller.online_manager)
    assert choices[0].tracks[choices[0].index] is tracks[2]
    tracks[2].extra = {}
    choices = controller.recommendations.choices(controller.online_manager)
    assert all("明确设置" not in c.reason for c in choices)


def test_schema_is_idempotent_and_future_version_is_preserved(tmp_path):
    import sqlite3
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
    memory = PreferenceStore(path)
    assert memory.profile() == {"explicit": {}, "artists": {}}
    assert memory.available is False
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_shuffled_collection_counts_selected_start_as_consumed(controller, monkeypatch, reverse):
    from app.music.queue import PlayMode
    monkeypatch.setattr("app.music.queue.random.shuffle", lambda rows: rows.reverse() if reverse else None)
    controller.queue.set_play_mode(PlayMode.SHUFFLE)
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    first = controller.get_current_song()["track_id"]
    following = controller.queue.next(auto=True)
    assert following["track_id"] != first
    assert controller.queue._shuffle_order[0] == 0


def test_cancel_after_ipc_ack_does_not_publish_stale_success(controller):
    events = []
    controller.event_bus.subscribe("music.recommendation_changed", events.append)
    def cancel_on_read(key, **kwargs):
        controller.recommendations.cancel()
        return None
    controller.mpv.get_property = cancel_on_read
    result = controller.recommend_music()
    assert result.status is OperationStatus.UNKNOWN
    assert not events


def test_cancel_while_confirming_movie_pause_never_loads_old_recommendation(controller):
    def pause_then_cancel():
        controller.recommendations.cancel()
        return True
    controller.set_movie_pauser(pause_then_cancel)
    result = controller.recommend_music()
    assert result.status is OperationStatus.UNKNOWN
    assert controller.mpv.loaded == []
    assert controller.recommendations.session is None


@pytest.mark.parametrize("fail", [False, True])
def test_preference_connection_closes_on_success_and_failure(tmp_path, monkeypatch, fail):
    import sqlite3
    original = sqlite3.connect
    opened = []
    def capture(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection
    monkeypatch.setattr("app.music.preferences.sqlite3.connect", capture)
    memory = PreferenceStore(tmp_path / "memory.db")
    def read(db):
        if fail:
            raise sqlite3.OperationalError("controlled read failure")
        return db.execute("SELECT 1").fetchone()[0]
    assert memory._run(read) == (None if fail else 1)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
