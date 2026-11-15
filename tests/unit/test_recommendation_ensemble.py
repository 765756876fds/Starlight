"""Both real controllers and stores share one bus; sources/players are offline adapters."""
from contextlib import closing
import hashlib
import sqlite3
import threading
import time

import pytest

from app.core.operation_result import OperationStatus
from app.movie.preferences import FilmMemory
from app.music.preferences import PreferenceStore
from app.voice.runtime import VoiceRuntime
from tests.unit.test_film_recommendation import movie as movie, film
from tests.unit.test_music_recommendation import controller as controller


@pytest.fixture
def ensemble(controller, movie, monkeypatch):
    cinema, _, picture = movie
    bus = controller.command_bus
    monkeypatch.setattr("app.core.command_bus.get_command_bus", lambda: bus)
    cinema._register_commands()
    cinema._music = controller
    controller.set_movie_pauser(cinema.pause_and_confirm)
    monkeypatch.setattr(controller.mpv, "is_running", lambda: bool(controller.mpv.loaded), raising=False)
    for player in (controller.mpv, picture):
        monkeypatch.setattr(player, "pause", lambda p=player: p.set_property("pause", True), raising=False)
        monkeypatch.setattr(player, "play", lambda p=player: p.set_property("pause", False), raising=False)
    picture.set_property("idle-active", True)
    runtime = VoiceRuntime({"asr": {}, "intent": {}})
    runtime.command_bus = bus
    runtime.music_controller_provider = lambda: controller
    runtime.movie_controller_provider = lambda: cinema
    runtime.music_mpv_provider = lambda: controller.mpv
    runtime.movie_mpv_provider = lambda: picture
    runtime._enqueue_speak = lambda *a, **k: None
    presented, workers = [], []
    cinema._recommendation_ready = presented.append
    execute = bus.execute_async
    def track(*args, **kwargs):
        worker = execute(*args, **kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(bus, "execute_async", track)
    def say(text, *, wait=True):
        start = len(workers)
        runtime._execute_intent(runtime.intent_router.parse(text), text)
        if wait:
            for worker in workers[start:]:
                worker.join(3)
                assert not worker.is_alive()
    yield controller, cinema, picture, runtime, say, presented, workers
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()


def test_music_film_and_context_share_bus_without_losing_collections(ensemble, monkeypatch):
    music, cinema, picture, runtime, say, presented, _ = ensemble
    say("我想听歌了")
    assert music.queue.size() == 10
    first_chart = music.recommendations.session["key"]
    say("先停一下")
    assert music.mpv.get_property("pause") is True
    say("接着放")
    assert music.mpv.get_property("pause") is False
    say("换个榜单")
    assert music.recommendations.session["key"] != first_chart
    say("来点伤感的")
    assert music.recommendations.session["style"] == "伤感"
    queue = music.queue.snapshot()
    say("我想看电影了")
    assert presented[-1][0].rows and not picture.loads
    assert music.queue.snapshot() == queue
    say("给我找几部高分悬疑电影")
    assert not presented[-1][0].rows and "数据不足" in presented[-1][0].message
    item = dict(vod_name="集成测试电影", type_name="电影", first_url="https://fixture.invalid/film")
    monkeypatch.setattr(cinema, "search", lambda query: [item])
    say("播放电影集成测试电影")
    assert picture.loads == [item["first_url"]]
    assert music.mpv.get_property("pause") is False  # load acknowledgement alone is insufficient
    cinema.tick()  # observes actual non-idle movie playback
    deadline = time.monotonic() + 1
    while music.mpv.get_property("pause") is not True and time.monotonic() < deadline:
        time.sleep(.005)
    assert music.mpv.get_property("pause") is True
    say("先停一下")
    assert picture.get_property("pause") is True
    say("接着放")
    assert picture.get_property("pause") is False and music.mpv.get_property("pause") is True
    # With both live, only an actual current GUI hint may resolve ambiguity.
    music.mpv.set_property("pause", False)
    runtime.media_context.note_focus("music")
    say("先停一下")
    assert music.mpv.get_property("pause") is True and picture.get_property("pause") is False
    assert music.queue.snapshot() == queue


def test_stores_restart_independently_and_preserve_manual_library(ensemble):
    music, cinema, picture, _, say, _, _ = ensemble
    playlist = music.db.create_playlist("手动歌单")
    saved = dict(title="手动保留曲目", artist="测试歌手", source_type="online", source_name="fixture", track_id="saved")
    assert music.db.add_to_playlist(playlist, saved)
    before = music.db.get_playlist_songs(playlist)
    assert music.music_memory.set_preference("artist", "测试歌手", 1)
    assert cinema.film_memory.remember(film(), True)
    item = dict(vod_name="剧集测试", type_name="电视剧", first_url="https://fixture.invalid/one",
                play_url="第一集$https://fixture.invalid/one#第二集$https://fixture.invalid/two")
    assert cinema.play(item, index=1)
    picture.set_property("time-pos", 45)
    cinema.tick()
    say("我想听歌了")
    assert music.music_memory.path != cinema.film_memory.path
    for _ in range(3):
        music_memory = PreferenceStore(music.music_memory.path)
        film_memory = FilmMemory(cinema.film_memory.path)
        assert music_memory.profile()["explicit"][("artist", "测试歌手")] == 1
        assert film_memory.profile("movie")["explicit"]
        assert film_memory.records("tv")[0]["episode"] == 1
        assert film_memory.records("tv")[0]["position"] == 45
    assert music.db.get_playlist_songs(playlist) == before
    for path in (music.music_memory.path, cinema.film_memory.path):
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("domain", ["music", "movie"])
@pytest.mark.parametrize("failure", ["corrupt", "future", "directory"])
def test_one_unavailable_store_never_breaks_other_domain(ensemble, tmp_path, domain, failure):
    music, cinema, _, _, _, _, _ = ensemble
    path = tmp_path / (domain + "-unavailable.db")
    if failure == "corrupt":
        path.write_bytes(b"controlled corrupt SQLite fixture")
    elif failure == "future":
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=99")
    else:
        path.mkdir()
    original = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    if domain == "music":
        memory = music.music_memory = PreferenceStore(path)
        music.recommendations.memory = memory
    else:
        memory = cinema.film_memory = FilmMemory(path)
        cinema.recommendations.memory = memory
    assert music.recommend_music().status is OperationStatus.CONFIRMED
    batch, current = cinema.recommendations.recommend()
    assert current() and batch.rows
    assert memory.available is False
    if original is not None:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    else:
        assert path.is_dir()
    healthy = cinema.film_memory if domain == "music" else music.music_memory
    assert healthy.available


def test_new_film_voice_invalidates_older_music_recommendation(ensemble, monkeypatch):
    music, _, picture, _, say, presented, workers = ensemble
    entered, release = threading.Event(), threading.Event()
    read = music.online_manager.get_top_lists
    def delayed():
        entered.set()
        assert release.wait(3)
        return read()
    monkeypatch.setattr(music.online_manager, "get_top_lists", delayed)
    try:
        say("我想听歌了", wait=False)
        assert entered.wait(1)
        say("我想看电影了")
        assert presented[-1][0].rows
    finally:
        release.set()
        for worker in workers:
            worker.join(3)
            assert not worker.is_alive()
    assert not music.mpv.loaded and not picture.loads
    assert music.queue.is_empty() and music.recommendations.session is None
