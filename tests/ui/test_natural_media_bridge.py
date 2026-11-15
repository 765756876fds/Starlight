"""Real queued Qt delivery and candidate identities, no network or private data."""

from types import SimpleNamespace
import threading

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QTabWidget, QWidget

from app.core.command_bus import Command, CommandBus, CommandType as C
from app.core.operation_result import OperationStatus as S
from app.movie.movie_controller import MovieController
from app.ui.widgets.media_voice_bridge import MediaVoiceBridge
from app.voice.media_context import MediaContext


@pytest.fixture
def scene():
    app = QApplication.instance() or QApplication([])
    tabs = QTabWidget()
    music, movie = QWidget(), QWidget()
    tabs._panels = {"music": music, "movie": movie}
    tabs.addTab(music, "音乐")
    tabs.addTab(movie, "影视")
    runtime = SimpleNamespace(command_bus=CommandBus())
    runtime.media_context = MediaContext(runtime)
    bridge = MediaVoiceBridge(runtime, tabs)
    calls = []
    music._show_queue = lambda: calls.append(("queue", QThread.currentThread()))
    music.btn_lyrics = QCheckBox(music)
    music._lyrics_window = QWidget(music)
    music._on_toggle_lyrics = lambda enabled: music._lyrics_window.setVisible(enabled)
    c = MovieController.__new__(MovieController)
    c._voice_candidate_lock = threading.RLock()
    c._voice_candidate_revision = 0
    c.recommendations = SimpleNamespace(_closed=False, _generation=1)
    c.is_playing = lambda: True
    movie._movie = c
    movie._recommendation_closed = False
    movie._video_win = SimpleNamespace(
        request_fullscreen=lambda: calls.append(("fullscreen", QThread.currentThread()))
    )
    movie._find_discovery_work = lambda row: calls.append((row["title"], QThread.currentThread()))
    movie._resume_recommended = lambda row, guard: calls.append(("resume", QThread.currentThread()))
    yield SimpleNamespace(
        app=app, tabs=tabs, music=music, movie=movie, bridge=bridge, runtime=runtime, calls=calls
    )
    bridge.closed = True
    tabs.close()
    tabs.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def posted(s, params, guard=None):
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            s.runtime.command_bus.execute(Command(C.MEDIA_UI, params, is_current=guard))
        )
    )
    worker.start()
    worker.join(0.5)
    assert not worker.is_alive()
    if guard is not None and not guard():
        assert results[0] is None  # CommandBus rejects before entering a handler.
    else:
        assert results[0].status is S.ACCEPTED
    QTest.qWait(30)


def test_existing_tools_reached_from_worker_on_gui_thread(scene):
    s = scene
    posted(s, {"action": "queue"})
    posted(s, {"action": "lyrics", "enabled": True})
    assert s.music.btn_lyrics.isChecked()
    assert not s.music._lyrics_window.isHidden()
    posted(s, {"action": "fullscreen", "enabled": True})
    assert [c[0] for c in s.calls] == ["queue", "fullscreen"]
    assert all(c[1] == s.app.thread() for c in s.calls)


def test_closed_or_obsolete_ui_request_does_nothing(scene):
    posted(scene, {"action": "queue"}, lambda: False)
    assert not scene.calls
    active = [True]
    assert (
        scene.bridge.post(
            Command(C.MEDIA_UI, {"action": "queue"}, is_current=lambda: active[0])
        ).status
        is S.ACCEPTED
    )
    active[0] = False
    QTest.qWait(30)
    assert not scene.calls
    scene.bridge.closed = True
    assert scene.bridge.post(Command(C.MEDIA_UI, {"action": "queue"})).status is S.UNAVAILABLE


def test_candidate_selected_by_identity_not_row_and_search_does_not_play(scene):
    s = scene
    movie = s.movie._movie
    rows = movie.publish_voice_candidates(
        [{"title": "同名电影", "year": 2001}, {"title": "同名电影", "year": 2024}]
    )
    candidate = movie.voice_candidate(1)
    assert movie.voice_candidate_row(candidate)["year"] == 2024
    movie.note_voice_candidate(rows[1]["_voice_candidate_id"])
    assert movie.voice_candidate() == candidate
    posted(s, {"action": "select", "candidate": candidate})
    assert [c[0] for c in s.calls] == ["同名电影"]  # existing search only; no player invoked
    movie.publish_voice_candidates([{"title": "另一批推荐"}])
    posted(s, {"action": "select", "candidate": candidate})
    assert len(s.calls) == 1
    fresh = movie.voice_candidate(0)
    movie.recommendations._generation += 1
    assert movie.voice_candidate_row(fresh) is None


def test_page_hint_tracks_existing_tabs(scene):
    assert scene.runtime.media_context.page_hint() == "music"
    scene.tabs.setCurrentIndex(1)
    assert scene.runtime.media_context.page_hint() == "movie"
