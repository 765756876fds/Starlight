"""Real command dispatch and Qt presentation, with isolated IPC/search substitutes."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSettings, QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from app.core.command_bus import Command, CommandBus, CommandType
from app.movie.movie_controller import MovieController
from app.ui.panels.movie_panel import MoviePanel
from app.ui.widgets.video_player_window import VideoPlayerWindow
from app.voice.intent import RuleBasedIntent


def wait_for(predicate):
    for _ in range(100):
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "Expected movie window state within one second"


@pytest.fixture
def scene(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from app.ui.theme.manager import get_theme_manager
    from app.ui.widgets import movie_home
    from app.movie.library import MovieLibrary

    get_theme_manager(app)
    bus = CommandBus()
    monkeypatch.setattr("app.core.command_bus.get_command_bus", lambda: bus)
    mpv = Mock()
    mpv.is_running.return_value = False

    def start(**_):
        mpv.is_running.return_value = True
        return True

    mpv.start.side_effect = start
    mpv.loadfile.return_value = {"error": "success"}
    mpv.stop.return_value = True
    mpv.get_property.side_effect = lambda key, *a, **k: {
        "time-pos": 0.0, "duration": 120.0, "pause": False,
        "idle-active": False, "track-list": [],
    }.get(key)
    monkeypatch.setattr("app.movie.movie_controller.MpvController", lambda *a, **k: mpv)
    monkeypatch.setattr(movie_home, "MovieLibrary", lambda _: MovieLibrary(tmp_path / "library.json"))
    monkeypatch.setattr(movie_home.MovieHome, "refresh_local", lambda *a, **k: None)
    monkeypatch.setattr(movie_home.MovieHome, "refresh_boards", lambda *a, **k: None)
    monkeypatch.setattr("app.ui.settings_store.UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr("app.ui.widgets.video_player_window.QSettings",
                        lambda *a: QSettings(str(tmp_path / "video.ini"), QSettings.IniFormat))
    monkeypatch.setattr(VideoPlayerWindow, "_apply_mpv_mouse_passthrough", lambda _: None)
    monkeypatch.setattr(VideoPlayerWindow, "raise_topmost", lambda _: None)
    controller = MovieController(source_url="https://source.example.invalid/api", config={
        "app.data_dir": str(tmp_path), "movie.embed": True,
    })
    item = {"vod_name": "测试电影", "first_url": "https://media.example.invalid/film.m3u8"}
    monkeypatch.setattr(controller, "search", Mock(return_value=[item]))
    panel = MoviePanel(movie=controller)
    panel._timer.stop()
    window = panel._video_win
    window._timer.stop()
    calls = []
    original = window.show_for_playback

    def show():
        calls.append(QThread.currentThread())
        original()

    monkeypatch.setattr(window, "show_for_playback", show)
    state = SimpleNamespace(app=app, bus=bus, controller=controller, panel=panel,
                            window=window, mpv=mpv, item=item, shows=calls)
    yield state
    panel._on_app_quit()
    window.close()
    panel.close()


def voice_command():
    intent = RuleBasedIntent().match("播放电影测试电影")
    assert intent.intent_type == CommandType.MOVIE_SEARCH_PLAY
    return Command(intent.intent_type, intent.params, source="voice")


@pytest.mark.parametrize("initial", ["first", "existing", "hidden", "minimized", "fullscreen", "fullscreen-minimized", "main-minimized"])
def test_real_voice_command_displays_reuses_and_restores_window(scene, initial):
    s = scene
    host = QWidget()
    if initial != "first":
        s.window.show()
    if initial == "hidden":
        s.window.hide()
    elif initial == "minimized":
        s.window.showMinimized()
    elif initial.startswith("fullscreen"):
        s.window.showFullScreen()
        if initial.endswith("minimized"):
            s.window.showMinimized()
    elif initial == "main-minimized":
        s.panel.setParent(host)
        host.showMinimized()
    try:
        assert s.bus.execute(voice_command()) is True
        wait_for(lambda: s.window.isVisible() and not s.window.isMinimized() and bool(s.shows))
        assert s.panel._video_win is s.window
        assert s.panel._embed_playing is True
        assert s.window._playing is True
        assert s.window._timer.isActive()
        assert s.shows == [s.app.thread()]
        assert s.controller.get_embed_wid() == s.window.container.native_id()
        s.mpv.start.assert_called_once_with(taskkill=False, wid=s.window.container.native_id())
        s.mpv.loadfile.assert_called_once_with(s.item["first_url"])
        assert s.controller.current_item() == s.item
        if initial.startswith("fullscreen"):
            assert s.window.isFullScreen()
            assert s.window._fullscreen
        if initial == "main-minimized":
            assert host.isMinimized()
    finally:
        s.panel.setParent(None)
        host.close()


def test_async_command_presents_only_on_gui_thread(scene):
    s = scene
    results = []
    worker = s.bus.execute_async(voice_command(), on_done=lambda result, error: results.append((result, error)))
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert results == [(True, None)]
    assert s.shows == []
    wait_for(lambda: bool(s.shows))
    assert s.shows == [s.app.thread()]
    assert s.window.isVisible()
    assert s.panel._embed_playing
    s.mpv.loadfile.assert_called_once()


@pytest.mark.parametrize("response", [{"error": "loading failed"}, None])
def test_failed_or_unconfirmed_load_does_not_present_window(scene, response):
    s = scene
    s.mpv.loadfile.return_value = response
    assert s.bus.execute(voice_command()) is False
    QTest.qWait(30)
    assert not s.window.isVisible()
    assert not s.shows
    assert not s.panel._embed_playing


def test_superseded_command_does_not_show_a_late_window(scene):
    s = scene
    current = threading.Event()
    current.set()
    command = voice_command()
    command.is_current = current.is_set
    worker = s.bus.execute_async(command)
    worker.join(timeout=1)
    assert not worker.is_alive()
    current.clear()
    QTest.qWait(30)
    assert not s.window.isVisible()
    assert not s.shows


def test_manual_play_still_shows_same_window_once(scene):
    s = scene
    s.panel._play_item(s.item)
    wait_for(lambda: bool(s.shows) and s.panel._embed_playing)
    assert s.panel._video_win is s.window
    assert s.window.isVisible()
    assert s.shows == [s.app.thread()]
    s.mpv.loadfile.assert_called_once()


def test_presentation_callback_failure_does_not_repeat_successful_play(scene):
    def failed(_):
        raise RuntimeError("presentation unavailable")

    scene.controller._voice_playback_ready = failed
    assert scene.bus.execute(voice_command()) is True
    scene.mpv.loadfile.assert_called_once()
    assert scene.controller.current_item() == scene.item


def test_two_voice_commands_reuse_the_same_window(scene):
    s = scene
    assert s.bus.execute(voice_command()) is True
    wait_for(lambda: len(s.shows) == 1)
    s.window.hide()
    assert s.bus.execute(voice_command()) is True
    wait_for(lambda: len(s.shows) == 2)
    assert s.panel._video_win is s.window
    assert s.window.isVisible()
    assert s.mpv.start.call_count == 1
    assert s.mpv.loadfile.call_count == 2


def test_stopped_movie_is_not_reopened_by_queued_presentation(scene):
    s = scene
    assert s.bus.execute(voice_command()) is True
    assert s.controller.stop() is True
    QTest.qWait(30)
    assert not s.window.isVisible()
    assert not s.shows
