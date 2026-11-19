"""Qt playback-bar integration with a temporary SQLite database and fake MPV."""
import threading
import time
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.core.event_bus import Event, EventType
from app.music.controller import MusicController
from app.ui.panels.music_panel import MusicPanel

pytestmark = pytest.mark.ui


def wait_for(predicate):
    app = QApplication.instance()
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


@pytest.fixture
def panel(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("app.music.controller.LxMusicClient", lambda: MagicMock(
        **{"is_connected.return_value": False}))
    mpv = MagicMock()
    mpv.is_playing.return_value = True
    controller = MusicController({"database": str(tmp_path / "music.db")}, mpv_adapter=mpv)
    widget = MusicPanel(music=controller)
    widget.show()
    try:
        yield widget, controller, mpv
    finally:
        widget._timer.stop()
        widget.close()
        wait_for(lambda: not widget.btn_like._task._running)
        wait_for(lambda: not widget._playlist_task._running)
        controller.db.close()
        app.processEvents()


def set_song(controller, song):
    controller._current_song = song
    controller.event_bus.publish(Event(event_type=EventType.MUSIC_STATE_CHANGED, data={"state": "queue_changed"}))


@pytest.mark.parametrize("online", [False, True])
def test_buttons_write_only_membership_and_refresh_the_visible_playlist(panel, online):
    widget, controller, mpv = panel
    song = dict(title="歌曲", artist="测试")
    if online:
        song.update(id=-1, source="online", source_name="First", track_id="10")
    else:
        song.update(id=controller.db.insert_song(dict(song, path="a.wav", filename="a.wav")))
    widget.tabs.setCurrentIndex(3)
    set_song(controller, song)
    wait_for(widget.btn_like.isEnabled)
    controller.queue.add(dict(id=42, title="队列保留"))
    position = controller.queue.size()
    mpv.reset_mock()

    QTest.mouseClick(widget.btn_like, Qt.LeftButton)
    assert not widget.btn_like.isEnabled()
    QTest.mouseClick(widget.btn_like, Qt.LeftButton)
    wait_for(widget.btn_like.isChecked)
    wait_for(lambda: widget.list_playlist_songs.count() == 1)
    assert controller.db.is_liked_song(song) is True
    QTest.mouseClick(widget.btn_like, Qt.LeftButton)
    wait_for(lambda: widget.btn_like.isEnabled() and not widget.btn_like.isChecked())
    wait_for(lambda: widget.list_playlist_songs.count() == 0)
    assert controller.db.is_liked_song(song) is False
    assert controller.queue.size() == position
    assert controller.get_current_song() is song
    mpv.loadfile.assert_not_called()
    mpv.set_property.assert_not_called()
    mpv.stop.assert_not_called()

    set_song(controller, None)
    wait_for(lambda: not widget.btn_like.isEnabled())
    assert not widget.btn_like.isChecked()


def test_late_save_never_applies_to_new_song_and_does_not_block_gui(panel, monkeypatch):
    widget, controller, _ = panel
    a, b = [dict(id=i, title=title) for i, title in [(1, "A"), (2, "B")]]
    set_song(controller, a)
    wait_for(widget.btn_like.isEnabled)
    original = controller.set_liked_song
    begun, release = threading.Event(), threading.Event()
    background_ids = []

    def write(song, liked):
        background_ids.append(threading.get_ident())
        begun.set()
        assert release.wait(2)
        return original(song, liked)

    monkeypatch.setattr(controller, "set_liked_song", write)
    try:
        widget.btn_like.click()
        wait_for(begun.is_set)
        assert background_ids != [threading.get_ident()]
        set_song(controller, b)
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(time.monotonic()))
        wait_for(lambda: bool(ticks))
        wait_for(lambda: widget.btn_like._song is b)
        assert not widget.btn_like.isChecked()
    finally:
        release.set()
    wait_for(widget.btn_like.isEnabled)
    assert not widget.btn_like.isChecked()
    assert controller.db.is_liked_song(a) is True
    assert controller.db.is_liked_song(b) is False


def test_ineligible_song_failed_write_and_closing_pending_read(panel, monkeypatch):
    widget, controller, _ = panel
    set_song(controller, dict(id=1, source="online", title="无标识"))
    wait_for(lambda: "标识" in widget.btn_like.toolTip())
    assert not widget.btn_like.isEnabled()

    song = dict(id=1, title="有标识")
    set_song(controller, song)
    wait_for(widget.btn_like.isEnabled)
    monkeypatch.setattr(controller, "set_liked_song", lambda *_: False)
    widget.btn_like.click()
    wait_for(lambda: "未更新" in widget.btn_like.toolTip())
    assert widget.btn_like.isEnabled() and not widget.btn_like.isChecked()
    assert controller.db.is_liked_song(song) is False

    begun, release = threading.Event(), threading.Event()
    def read(_song):
        begun.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(controller, "is_liked_song", read)
    try:
        set_song(controller, dict(id=2, title="不应被渲染"))
        wait_for(begun.is_set)
        assert widget.close()
    finally:
        release.set()
    wait_for(lambda: not widget.btn_like._task._running)
    assert not widget.btn_like.isEnabled() and not widget.btn_like.isChecked()
