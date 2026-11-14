"""Real isolated SQLite and controller, fixture public metadata, no network or private media."""
from contextlib import closing
from copy import deepcopy
import random
import sqlite3
import threading
from unittest.mock import patch

import pytest

from app.core.command_bus import Command, CommandBus, CommandType as C
from app.movie.catalog_metadata import bilibili_metadata
from app.movie.movie_controller import MovieController
from app.movie.preferences import FilmMemory, work_key
from app.movie.recommendation import FilmRecommendations
from app.voice.intent import IntentRouter
from app.voice.runtime import VoiceRuntime


def film(number=1, kind="movie", genre="科幻", year=2024):
    return dict(title=f"影片{number}", kind=kind, year=year, region="中国", genres=[genre],
                score=8.5, score_source="测试平台", sources=[dict(platform="fixture", platform_name="测试平台",
                source_id=str(number), rank=number, url="https://fixture.invalid/film", board="热榜")])


class Catalog:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def read(self, kind):
        self.calls.append(kind)
        return {"works": deepcopy(self.rows), "states": [{"state": "success"}]}


def service(tmp_path, rows=None):
    memory = FilmMemory(tmp_path / "preferences.db")
    return FilmRecommendations(None, memory, Catalog(rows or [film(i) for i in range(1, 9)]),
                               metadata=lambda *a, **k: [], rng=random.Random(4))


def test_cold_start_uses_real_catalog_contract_and_varies_top_candidates(tmp_path):
    s = service(tmp_path)
    first = [s.recommend()[0].rows[0]["title"] for _ in range(25)]
    assert len(set(first)) > 1
    assert s.discovery.calls == ["movie"] * 25
    assert s.memory.profile("movie") == {"explicit": {}, "inferred": {}}


def test_actual_history_changes_ranking_explicit_preference_wins(tmp_path):
    comedy, sci = film(1, genre="喜剧"), film(2)
    s = service(tmp_path, [comedy, sci])
    s.memory.observe(film(3), {"vod_name": "已观看"}, 60, 120)
    assert s.recommend()[0].rows[0]["title"] == sci["title"]
    s.memory.set_preference("movie", "genre", "喜剧", True)
    assert s.recommend()[0].rows[0]["title"] == comedy["title"]
    assert s.memory.profile("tv")["inferred"] == {}


def test_current_genre_and_real_rating_filter_missing_data_truthfully(tmp_path):
    s = service(tmp_path)
    s.memory.set_preference("movie", "genre", "科幻", True)
    s.metadata = lambda *a, **k: [dict(id="a", title="悬疑作品", rate="8.8"),
                                dict(id="b", title="低评分", rate="7.0"),
                                dict(id="c", title="无评分", rate="")]
    batch, _ = s.recommend(genre="悬疑", min_score=8)
    assert [r["title"] for r in batch.rows] == ["悬疑作品"]
    assert batch.rows[0]["score_source"] == "豆瓣"
    assert batch.rows[0]["tag_basis"] == "豆瓣分类标签"
    s.metadata = lambda *a, **k: []
    batch, _ = s.recommend(genre="悬疑", min_score=8)
    assert batch.rows == [] and "数据不足" in batch.message


def test_current_genre_can_override_a_saved_genre_dislike(tmp_path):
    s = service(tmp_path)
    s.memory.set_preference("movie", "genre", "科幻", False)
    assert s.recommend()[0].rows == []
    assert s.recommend(genre="科幻")[0].rows


def test_like_content_does_not_claim_user_explicitly_set_all_its_tags(tmp_path):
    s = service(tmp_path)
    assert s.feedback(film(), True)
    profile = s.memory.profile("movie")
    assert profile["explicit"][("content", work_key(film()))] == 1
    assert ("genre", "科幻") not in profile["explicit"]
    assert profile["inferred"][("genre", "科幻")] > 0


def test_unwatched_first_partial_watch_excluded_negative_feedback_exact(tmp_path):
    s = service(tmp_path, [film(1), film(2), film(3)])
    s.memory.observe(film(1), {}, 100, 100, ended=True)
    s.memory.observe(film(2), {"first_url": "https://fixture.invalid/2"}, 35, 100)
    batch, _ = s.recommend()
    assert [r["title"] for r in batch.rows] == ["影片3", "影片1"]
    assert s.feedback(film(3), False)
    assert [r["title"] for r in s.recommend()[0].rows] == ["影片1"]
    assert s.memory.profile("movie")["explicit"][("content", work_key(film(3)))] == -1


def test_identity_preserves_remakes_seasons_and_source_uncertainty():
    original = film()
    assert work_key(original) != work_key(dict(original, year=1999))
    assert work_key(dict(original, kind="tv", season=1)) != work_key(dict(original, kind="tv", season=2))
    assert work_key(dict(original, year=None)) != work_key(dict(original, year=None, sources=[]))


def test_search_and_selection_are_saved_without_becoming_preferences(tmp_path):
    memory = FilmMemory(tmp_path / "memory.db")
    assert memory.record_action("search", "测试片名")
    assert memory.record_action("select", "测试片名", work=film())
    assert not memory.record_action("search", "https://fixture.invalid/private")
    with closing(sqlite3.connect(memory.path)) as db:
        assert db.execute("SELECT action,domain FROM actions ORDER BY id").fetchall() == [("search", "unknown"), ("select", "movie")]
    assert FilmMemory(memory.path).profile("movie") == {"explicit": {}, "inferred": {}}


def test_verified_cross_platform_history_excludes_partial_but_not_remakes(tmp_path):
    original = film()
    other = dict(original, sources=[dict(platform="other", platform_name="其他平台", source_id="x", rank=1)])
    remake = dict(other, year=1999)
    s = service(tmp_path, [other, remake])
    s.memory.observe(original, {"first_url": "https://fixture.invalid/current"}, 40, 100)
    assert [row["year"] for row in s.recommend()[0].rows] == [1999]
    other_service = service(tmp_path / "explicit", [other, remake])
    assert other_service.feedback(original, False)
    assert [row["year"] for row in other_service.recommend()[0].rows] == [1999]


def test_memory_persists_repeated_initialization_and_never_infers_detail_open(tmp_path):
    path = tmp_path / "memory.db"
    first = FilmMemory(path)
    assert first.set_preference("anime", "genre", "动画", True)
    first.observe(film(kind="anime"), {}, 1, 100)
    second = FilmMemory(path)
    assert second.profile("anime")["explicit"][("genre", "动画")] == 1
    assert second.profile("anime")["inferred"] == {}
    second.observe(film(kind="anime"), {}, 50, 100)
    assert second.profile("anime")["inferred"][("genre", "科幻")] == .5
    second.observe(film(kind="anime"), {}, 60, 100)
    assert second.profile("anime")["inferred"][("genre", "科幻")] == .5
    assert second.profile("movie")["explicit"] == {}


@pytest.mark.parametrize("mode", ["corrupt", "directory", "future"])
def test_memory_failure_preserves_original_and_degrades(tmp_path, mode):
    path = tmp_path / "memory.db"
    if mode == "corrupt":
        path.write_bytes(b"original corrupt evidence")
    elif mode == "directory":
        path.mkdir()
    else:
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=9")
    original = path.read_bytes() if path.is_file() else None
    memory = FilmMemory(path)
    assert memory.profile("movie") == {"explicit": {}, "inferred": {}}
    assert not memory.set_preference("movie", "genre", "科幻")
    assert not memory.available
    if original:
        assert path.read_bytes() == original


@pytest.mark.parametrize("fail", [False, True])
def test_memory_closes_connections_even_when_reader_fails(tmp_path, monkeypatch, fail):
    connections = []
    connect = sqlite3.connect
    def capture(*a, **k):
        db = connect(*a, **k)
        connections.append(db)
        return db
    monkeypatch.setattr("app.movie.preferences.sqlite3.connect", capture)
    def read(db):
        if fail:
            raise sqlite3.OperationalError("controlled failure")
        return 1
    memory = FilmMemory(tmp_path / "memory.db")
    assert memory._run(read) == (None if fail else 1)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("value,expected", [("9.9分", 9.9), ({"score": 8.5}, 8.5),
    (None, None), ("热度99", None), (True, None), ("0分", None), ("11分", None)])
def test_public_rating_only_uses_valid_score_fields(value, expected):
    assert bilibili_metadata({"rating": value})["score"] == expected


@pytest.mark.parametrize("kind", ["tv", "anime"])
def test_series_resume_keeps_episode_and_advances_only_after_real_eof(tmp_path, kind):
    s = service(tmp_path)
    work = film(kind=kind)
    item = {"vod_name": "剧集", "first_url": "https://fixture.invalid/1",
            "play_url": "第一集$https://fixture.invalid/1#第二集$https://fixture.invalid/2",
            "Cookie": "never persist", "headers": {"secret": "never persist"}}
    s.memory.observe(work, item, 50, 100, 0)
    row = s.continuing(kind)[0].rows[0]
    assert row["resume_seconds"] == 50 and row["_resume_episode"] == 0
    assert "Cookie" not in row["_resume_item"] and "headers" not in row["_resume_item"]
    s.memory.observe(work, item, 100, 100, 0, ended=True)
    row = s.continuing(kind)[0].rows[0]
    assert row["resume_seconds"] == 0 and row["_resume_episode"] == 1
    s.memory.observe(work, item, 100, 100, 1, ended=True)
    assert s.continuing(kind)[0].rows == []


def test_cancel_old_result_and_source_partial_success(tmp_path):
    s = service(tmp_path)
    read = s.discovery.read
    def cancelled(kind):
        result = read(kind)
        s.cancel()
        return result
    s.discovery.read = cancelled
    batch, current = s.recommend()
    assert not current() and batch.rows == []
    s.discovery.read = lambda kind: dict(read(kind), states=[{"state": "success"}, {"state": "failed"}])
    batch, current = s.recommend()
    assert current() and batch.rows and "部分来源" in batch.message


def test_explicit_feedback_is_one_connection_and_atomic(tmp_path, monkeypatch):
    s = service(tmp_path)
    original, count = sqlite3.connect, []
    def connect(*args, **kwargs):
        count.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr("app.movie.preferences.sqlite3.connect", connect)
    assert s.feedback(film(), True)
    assert len(count) == 1
    assert s.memory.profile("movie")["explicit"][("content", work_key(film()))] == 1


def test_one_recommendation_reads_memory_as_one_snapshot(tmp_path, monkeypatch):
    s = service(tmp_path)
    original, count = sqlite3.connect, []
    def connect(*args, **kwargs):
        count.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr("app.movie.preferences.sqlite3.connect", connect)
    assert s.recommend()[0].rows
    assert len(count) == 1


def test_feedback_failure_rolls_back_all_explicit_preferences(tmp_path):
    s = service(tmp_path)
    s.memory.profile("movie")
    with closing(sqlite3.connect(s.memory.path)) as db, db:
        db.execute("CREATE TRIGGER fail_feedback BEFORE INSERT ON feedback "
                   "BEGIN SELECT RAISE(ABORT,'controlled failure'); END")
    assert not s.feedback(film(), True)
    with closing(sqlite3.connect(s.memory.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM preferences").fetchone()[0] == 0


class Player:
    def __init__(self):
        self._properties = {"idle-active": False, "time-pos": 0, "duration": 100, "pause": False}
        self.loads = []
    def is_running(self):
        return True
    def get_property(self, key, **kwargs):
        return self._properties.get(key)
    def get_idle_active(self):
        return self._properties["idle-active"]
    def loadfile(self, url):
        self.loads.append(url)
        self._properties.update({"path": url, "time-pos": 0, "idle-active": False})
        return {"error": "success"}
    def set_property(self, key, value):
        self._properties[key] = value
    def seek(self, value, mode):
        self._properties["time-pos"] = value
    def stop_process(self):
        pass


@pytest.fixture
def movie(tmp_path, monkeypatch):
    bus = CommandBus()
    monkeypatch.setattr("app.core.command_bus.get_command_bus", lambda: bus)
    config = {"movie.history_db": str(tmp_path / "history.db"), "app.data_dir": str(tmp_path), "movie.embed": False}
    player = Player()
    with patch("app.movie.movie_controller.MpvController", return_value=player):
        ctrl = MovieController(source_url="https://fixture.invalid/api", config=config)
    ctrl.recommendations.discovery = Catalog([film(1), film(2)])
    ctrl.recommendations.metadata = lambda *a, **k: []
    yield ctrl, bus, player
    ctrl.close()


def test_voice_runtime_bus_controller_presents_without_playing(movie):
    ctrl, bus, player = movie
    presented, done = [], threading.Event()
    def present(payload):
        presented.append(payload)
        done.set()
    ctrl._recommendation_ready = present
    rt = VoiceRuntime({"asr": {}, "intent": {}})
    rt.command_bus = bus
    rt._enqueue_speak = lambda *a, **k: None
    rt._execute_intent(rt.intent_router.parse("我想看电影了"), "我想看电影了")
    assert done.wait(2)
    batch, current, _ = presented[0]
    assert batch.rows and current()
    assert player.loads == []


def test_new_discovery_request_invalidates_pending_voice_movie_search(movie):
    ctrl, bus, player = movie
    entered, release = threading.Event(), threading.Event()
    def search(query):
        entered.set()
        assert release.wait(2)
        return [dict(vod_name="旧电影", first_url="https://fixture.invalid/old")]
    ctrl.search = search
    task = bus.execute_async(Command(C.MOVIE_SEARCH_PLAY, params={"query": "旧电影"}, source="voice"))
    try:
        assert entered.wait(1)
        ctrl.recommendations.begin()
    finally:
        release.set()
        task.join(2)
    assert not task.is_alive()
    assert player.loads == []


def test_controller_progress_resume_and_next_episode_keep_legacy_path(movie):
    ctrl, bus, player = movie
    item = dict(vod_name="剧集", type_name="电视剧", first_url="https://fixture.invalid/1",
                play_url="一$https://fixture.invalid/1#二$https://fixture.invalid/2")
    assert ctrl.play(item)
    player._properties["time-pos"] = 42
    ctrl.tick()
    records = ctrl.film_memory.records("tv")
    assert records[0]["position"] == 42
    batch, _ = ctrl.recommendations.continuing("tv")
    saved = batch.rows[0]
    assert ctrl.play(dict(saved["_resume_item"], _resume_position=saved["resume_seconds"]), index=0)
    ctrl.tick()
    assert player._properties["time-pos"] == 42
    player._properties["idle-active"] = True
    ctrl.tick()
    assert player.loads[-1] == "https://fixture.invalid/2"
    assert ctrl.current()["play_url"] == item["play_url"]


def test_external_movie_can_pause_resume_via_existing_handlers(movie):
    ctrl, bus, player = movie
    player.pause = lambda: player._properties.update(pause=True)
    player.play = lambda: player._properties.update(pause=False)
    assert ctrl.play_external_url("https://fixture.invalid/external")
    assert ctrl.current() is None and ctrl._external_url_mode
    assert bus.execute(Command(C.MOVIE_PAUSE)) is True
    assert player._properties["pause"] is True
    assert bus.execute(Command(C.MOVIE_RESUME)) is True
    assert player._properties["pause"] is False


def test_external_play_invalidates_old_recommendation_and_cannot_learn_old_item(movie):
    ctrl, _, player = movie
    _, current = ctrl.recommendations.recommend()
    ctrl._current = dict(vod_name="旧剧集", type_name="电视剧")
    assert ctrl.play_external_url("https://fixture.invalid/new-external")
    assert not current()
    player._properties["time-pos"] = 45
    ctrl._remember_view(force=True)
    records = ctrl.film_memory.records("tv")
    assert len(records) == 1 and records[0]["position"] == 0 and records[0]["state"] == "opened"
    assert ctrl.film_memory.profile("tv")["inferred"] == {}


@pytest.mark.parametrize("phrase,expected,params", [
    ("我想看电影了", C.MOVIE_RECOMMEND, {"kind": "movie"}),
    ("我想看电视剧", C.MOVIE_RECOMMEND, {"kind": "tv"}),
    ("给我找几部高分悬疑电影", C.MOVIE_RECOMMEND, {"kind": "movie", "genre": "悬疑", "min_score": 8}),
    ("来点科幻电影", C.MOVIE_RECOMMEND, {"kind": "movie", "genre": "科幻"}),
    ("继续看上次的电视剧", C.MOVIE_CONTINUE, {"kind": "tv"}),
])
def test_film_intents(phrase, expected, params):
    result = IntentRouter({}).parse(phrase)
    assert result.intent_type == expected and result.params == params


@pytest.mark.parametrize("phrase", ["不要来点科幻电影", "我想看电影了是什么意思", "他说来点科幻电影",
    "我想看电影了？", "先停一下？", "不要先停一下", "先停一下然后播放电影测试", "给我找几部高分悬疑电影是什么意思"])
def test_new_phrases_preserve_question_negation_and_narration_safety(phrase):
    result = IntentRouter({}).parse(phrase)
    assert result.intent_type in ("system.unknown", "system.clarify")
