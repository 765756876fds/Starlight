"""Real Qt events with isolated film memory and explicitly simulated public metadata."""
from copy import deepcopy
from types import SimpleNamespace
import threading
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from app.core.command_bus import CommandBus, CommandType as C
from app.movie.movie_controller import MovieController
from app.movie.preferences import FilmMemory, work_key
from app.movie.recommendation import FilmBatch, FilmRecommendations
from app.ui.panels.movie_panel import MoviePanel
from app.ui.widgets.media_focus import MediaFocusTracker
from app.voice.runtime import VoiceRuntime
from tests.ui.test_movie_card_interactions import scene as scene
from tests.ui.test_media_homes import qapp as qapp, wait
from tests.unit.test_film_recommendation import film


def connect(scene, tmp_path):
    threads = []
    class Catalog:
        def read(self, kind):
            threads.append(QThread.currentThread())
            return {"works": [film(i, kind=kind) for i in range(1, 9)], "states": [{"state": "success"}]}
    service = FilmRecommendations(scene.backend, FilmMemory(tmp_path / "film.db"), Catalog(),
                                  metadata=lambda *a, **k: [])
    scene.backend.recommendations = service
    scene.backend.episodes = lambda item: MovieController.episodes(scene.backend, item)
    scene.backend.get_history = lambda: []
    return service, threads


def voice(scene, text):
    runtime = VoiceRuntime({"asr": {}, "intent": {}})
    bus = CommandBus()
    runtime.command_bus = bus
    runtime._enqueue_speak = Mock()
    for command, resume in ((C.MOVIE_RECOMMEND, False), (C.MOVIE_CONTINUE, True)):
        bus.register(command, lambda c, r=resume: MovieController._cmd_recommend(scene.backend, c, r))
    runtime._execute_intent(runtime.intent_router.parse(text), text)
    return runtime


def test_button_displays_six_candidates_off_main_thread_without_playback(scene, tmp_path, qapp):
    service, threads = connect(scene, tmp_path)
    QTest.mouseClick(scene.home.recommend_button, Qt.LeftButton)
    wait(lambda: scene.home.recommendation_section.cards.count() == 6)
    assert all(t != qapp.thread() for t in threads)
    assert list(scene.home.nav_buttons) == ["movie", "tv", "anime", "variety", "all", "local"]
    scene.panel._play_item.assert_not_called()
    assert service.memory.profile("movie")["inferred"] == {}


def test_presented_kind_and_partial_source_status_stay_visible_on_retry(scene, tmp_path):
    service, _ = connect(scene, tmp_path)
    calls = []
    read = service.discovery.read
    def observed(kind):
        calls.append(kind)
        return read(kind)
    service.discovery.read = observed
    scene.home.present_recommendations(FilmBatch([film(kind="tv")], "部分来源暂不可用，已展示可用结果", "tv"))
    assert scene.home.recommendation_kind.currentData() == "tv"
    assert scene.home.recommendation_section.hint.isVisible()
    assert "部分来源" in scene.home.recommendation_section.hint.text()
    QTest.mouseClick(scene.home.recommendation_section.all_button, Qt.LeftButton)
    wait(lambda: not scene.home._recommendation_task._running)
    assert calls == ["tv"]


def test_voice_runtime_real_bus_queued_presentation_is_gui_only(scene, tmp_path, qapp):
    _, threads = connect(scene, tmp_path)
    rendered = []
    present = scene.home.present_recommendations
    def observed(batch):
        rendered.append(QThread.currentThread())
        present(batch)
    scene.home.present_recommendations = observed
    runtime = voice(scene, "我想看电影了")
    wait(lambda: scene.home.recommendation_section.cards.count() == 6)
    assert rendered == [qapp.thread()]
    assert threads[0] != qapp.thread()
    scene.panel._play_item.assert_not_called()
    runtime._enqueue_speak.assert_not_called()


def test_navigation_invalidates_late_recommendation(scene, tmp_path):
    service, _ = connect(scene, tmp_path)
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)
    read = service.discovery.read
    def slow(kind):
        entered.set()
        assert release.wait(2)
        return read(kind)
    service.discovery.read = slow
    scene.home.request_recommendations()
    assert entered.wait(1)
    scene.home.show_category("tv")
    release.set()
    wait(lambda: not scene.home._recommendation_task._running)
    assert scene.home._view == "tv"
    assert scene.home.recommendation_section.cards.count() == 0


def test_rapid_kind_changes_keep_only_latest_candidates(scene, tmp_path):
    service, _ = connect(scene, tmp_path)
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)
    read = service.discovery.read
    def slow(kind):
        if kind == "movie":
            entered.set()
            assert release.wait(2)
        return read(kind)
    service.discovery.read = slow
    scene.home.request_recommendations()
    assert entered.wait(1)
    scene.home.recommendation_kind.setCurrentIndex(1)
    scene.home.request_recommendations()
    release.set()
    wait(lambda: not scene.home._recommendation_task._running and scene.home.recommendation_section.cards.count() == 6)
    assert {scene.home.recommendation_section.cards.item(i).data(Qt.UserRole)["kind"] for i in range(6)} == {"tv"}


def test_leaving_movie_page_invalidates_late_result(scene, tmp_path):
    service, _ = connect(scene, tmp_path)
    batch, current = service.recommend()
    assert current()
    scene.panel.hide()
    assert not current()
    scene.panel._on_film_recommendations((batch, current, ""))
    assert scene.home.recommendation_section.cards.count() == 0


def test_feedback_snapshots_selected_work_and_does_not_learn_from_details(scene, tmp_path):
    service, _ = connect(scene, tmp_path)
    first, second = film(1), film(2)
    scene.home.present_recommendations(FilmBatch([first, second], "测试数据"))
    card = scene.home.recommendation_section.cards.item(0)
    scene.home._select_work(card)
    assert service.memory.profile("movie")["explicit"] == {}
    QTest.mouseClick(scene.home.like_button, Qt.LeftButton)
    scene.home._selected_work = deepcopy(second)
    wait(lambda: not scene.home._preference_task._running)
    profile = service.memory.profile("movie")["explicit"]
    assert profile[("content", work_key(first))] == 1
    assert ("content", work_key(second)) not in profile
    scene.panel._play_item.assert_not_called()


def test_unique_series_resume_uses_existing_window_play_entry(scene, tmp_path):
    service, _ = connect(scene, tmp_path)
    item = dict(vod_name="剧集", first_url="https://fixture.invalid/1",
                play_url="第一集$https://fixture.invalid/1#第二集$https://fixture.invalid/2")
    service.memory.observe(film(kind="tv"), item, 42, 100, 1)
    voice(scene, "继续看上次的电视剧")
    wait(lambda: scene.panel._play_item.call_count == 1)
    args, kwargs = scene.panel._play_item.call_args
    assert args[0]["_resume_position"] == 42
    assert args[0]["play_url"] == item["play_url"]
    assert kwargs["index"] == 1 and kwargs["is_current"]()


def test_ambiguous_resume_waits_for_selection(scene, tmp_path):
    service, _ = connect(scene, tmp_path)
    for i in (1, 2):
        service.memory.observe(film(i, kind="tv"), {"vod_name": f"剧{i}", "first_url": f"https://fixture.invalid/{i}"}, 42, 100)
    voice(scene, "继续看上次的电视剧")
    wait(lambda: scene.home.recommendation_section.cards.count() == 2)
    scene.panel._play_item.assert_not_called()
    scene.home._activate_recommendation(scene.home.recommendation_section.cards.item(1))
    scene.panel._play_item.assert_called_once()


def test_shutdown_and_deferred_resume_cannot_start_old_movie(scene, tmp_path):
    connect(scene, tmp_path)
    valid = [True]
    scene.panel._video_win = SimpleNamespace()
    scene.panel._do_embed_play = Mock()
    MoviePanel._play_item(scene.panel, {"vod_name": "旧请求"}, is_current=lambda: valid[0])
    valid[0] = False
    QTest.qWait(20)
    scene.panel._do_embed_play.assert_not_called()
    scene.panel._video_win = None
    scene.home.shutdown()
    scene.panel._on_film_recommendations((FilmBatch([film()], "late"), lambda: True, ""))
    assert scene.home.recommendation_section.cards.count() == 0


def test_focus_tracker_uses_gui_events_without_reading_mpv(qapp):
    target = QWidget()
    target.setProperty("starlightMediaDomain", "music")
    context = SimpleNamespace(note_focus=Mock())
    tracker = MediaFocusTracker(context, target)
    QCoreApplication.sendEvent(target, QFocusEvent(QEvent.FocusIn))
    context.note_focus.assert_called_with("music")
    tracker.deleteLater()
    target.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
