from unittest.mock import Mock

import pytest
from PySide6.QtCore import QAbstractAnimation, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPixmap, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.music.online.models import OnlineTrack
from app.ui.theme.manager import get_theme_manager
from tests.ui.test_music_search_reliability import panel, qapp, wait_until


def wheel(view, *, angle=0, pixel=0):
    target = view.viewport()
    local = target.rect().center()
    event = QWheelEvent(QPointF(local), QPointF(target.mapToGlobal(local)),
                        QPoint(0, pixel), QPoint(0, angle), Qt.NoButton,
                        Qt.NoModifier, Qt.ScrollUpdate, False)
    QApplication.sendEvent(target, event)


@pytest.mark.parametrize("width,grid,collection", [
    (850, True, "search"), (1100, False, "search"), (850, True, "local"), (1100, False, "playlist"),
])
def test_mouse_and_trackpad_wheel_distance_with_real_qt_events(panel, width, grid, collection):
    p, music, errors = panel
    tracks = [OnlineTrack("fixture", str(i), f"歌曲 {i}") for i in range(100)]
    p.resize(width, 620)
    p.show()
    p.btn_view_toggle.setChecked(grid)
    p._toggle_view_mode(grid)
    if collection == "search":
        p.tabs.setCurrentIndex(1)
        p._on_search_done({"fixture": tracks})
        p.online_stack.setCurrentWidget(p.results_page)
        listing = p.list_results
    else:
        songs = [dict(title=track.title, source_type="local", id=i) for i, track in enumerate(tracks, 1)]
        if collection == "local":
            p.tabs.setCurrentIndex(0)
            p._show_lyric_results(songs, "样本")
            listing = p.list_local
        else:
            p.tabs.setCurrentIndex(3)
            p._apply_playlists(None, ([{"id": 1, "name": "样本"}], 1, songs), None)
            listing = p.list_playlist_songs
    QApplication.processEvents()
    listing.setCurrentRow(0)
    bar = listing.verticalScrollBar()
    assert bar.maximum() > 250
    bar.setValue(40)
    wheel(listing, angle=-120)
    assert bar.value() - 40 == QApplication.wheelScrollLines() * 40
    before = bar.value()
    wheel(listing, pixel=-17, angle=-120)
    assert bar.value() - before == 17
    assert listing.currentRow() == 0 and not errors


def test_wheel_on_discovery_preview_moves_the_outer_scroll_area(panel):
    p, music, errors = panel
    p.resize(850, 550)
    p.show()
    p.tabs.setCurrentIndex(1)
    p.online_stack.setCurrentWidget(p.discovery)
    p.discovery.pages.setCurrentWidget(p.discovery.home)
    preview = p.discovery.sections["today"].cards
    preview.set_rows([{"title": "一个可点击的封面"}], lambda row: row)
    QTest.qWait(20)
    outer = p.discovery.home.verticalScrollBar()
    outer.setValue(0)
    assert outer.maximum() > 40
    wheel(preview, angle=-120)
    assert outer.value() >= 40
    assert not errors


def test_discovered_album_click_keeps_display_order_and_duplicate_occurrences(panel):
    p, music, errors = panel
    first = OnlineTrack("甲", "1", "第一首")
    second = OnlineTrack("乙", "2", "第二首")
    tracks = [first, second, first]
    music.play_online_list = Mock()
    p._play_async = Mock()
    p._show_discovered_tracks(tracks, "样本专辑")
    assert p.list_results.count() == len(tracks)
    p.list_results.itemDoubleClicked.emit(p.list_results.item(2))
    assert p._play_async.call_args.args == (music.play_online_list, tuple(tracks), 2)
    assert not errors


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_immersive_lyrics_restores_search_selection_and_scroll(panel, qapp, theme):
    p, music, errors = panel
    manager = get_theme_manager(qapp)
    old = manager.mode
    manager.set_theme(theme, persist=False)
    try:
        p.resize(850, 650)
        p.show()
        p.tabs.setCurrentIndex(1)
        p._on_search_done({"fixture": [OnlineTrack("fixture", str(i), f"标题 {i}") for i in range(80)]})
        p.online_stack.setCurrentWidget(p.results_page)
        p.list_results.setCurrentRow(31)
        bar = p.list_results.verticalScrollBar()
        QTest.qWait(20)
        bar.setValue(202)
        assert bar.value() == 202
        QTest.mouseClick(p.btn_playing_lyrics, Qt.LeftButton)
        assert p.playing_lyrics.isVisible() and not p.tabs.isVisible()
        assert not p._media_pane.isVisible() and p.btn_queue.isVisible()
        QTest.mouseClick(p.btn_playing_lyrics, Qt.LeftButton)
        assert p.list_results.isVisible() and not p.playing_lyrics.isVisible()
        assert p.list_results.count() == 80 and p.list_results.currentRow() == 31
        assert bar.value() == 202 and p.online_stack.currentWidget() is p.results_page
        assert not errors
    finally:
        manager.set_theme(old, persist=False)


def test_cover_generation_and_paused_scroll_follow_the_shared_clock(panel, monkeypatch):
    p, music, errors = panel
    music.is_playing = Mock(return_value=True)
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)

    class Covers(QObject):
        loaded = Signal(str)

        def __init__(self):
            super().__init__()
            self.images = {}
            self.requests = []

        def get(self, key):
            return self.images.get(key)

        def request(self, key):
            self.requests.append(key)

        def complete(self, key, color):
            pixmap = QPixmap(100, 100)
            pixmap.fill(QColor(color))
            self.images[key] = pixmap
            self.loaded.emit(key)

    covers = Covers()
    monkeypatch.setattr("app.ui.widgets.playing_lyrics._get_cover_loader", lambda: covers)
    p.resize(850, 650)
    p.show()
    p.btn_playing_lyrics.click()
    engine, view = p._lyrics_window, p.playing_lyrics
    engine._timer.stop()
    lines = [(i, f"第 {i} 行") for i in range(30)]
    for title, key in (("旧歌", "a"), ("新歌", "b")):
        engine.current_track = {"artist": title, "cover_url": key}
        engine.set_lyrics(lines, title)
    assert covers.requests == ["net:a", "net:b"] and view.title.text() == "新歌"
    covers.complete("net:a", "red")
    assert view._wash.isNull()
    covers.complete("net:b", "royalblue")
    assert not view._wash.isNull()
    assert view._cover_fade.state() == QAbstractAnimation.Running
    shown = view._wash.cacheKey()
    displayed_cover = view.stage.artwork.cacheKey()
    engine.current_track = {"artist": "第三首", "cover_url": "c"}
    engine.set_lyrics(lines, "第三首")
    assert view._cover_wait.isActive() and view.art.text() == "♪"
    assert view._wash.cacheKey() == shown
    assert view.stage.artwork.cacheKey() == displayed_cover
    covers.complete("net:b", "darkgreen")
    assert view._cover_wait.isActive() and view._wash.cacheKey() == shown
    assert view.stage.artwork.cacheKey() == displayed_cover
    covers.complete("net:c", "tomato")
    assert view.title.text() == "第三首" and view._wash.cacheKey() != shown
    assert view.stage.artwork.cacheKey() == covers.images["net:c"].cacheKey()
    assert not view._cover_wait.isActive() and not view._previous_wash.isNull()
    engine.current_track = {"artist": "无图", "cover_url": "missing"}
    engine.set_lyrics(lines, "无图")
    wait_until(lambda: view._wash.isNull() and view._previous_wash.isNull())
    assert view.stage.artwork.isNull() and view.stage.previous_field.isNull()
    assert view.art.text() == "♪" and not view._cover_wait.isActive()
    QTest.qWait(20)
    assert view.pages.currentWidget() is view.stage
    view.reader_action.trigger()
    QTest.qWait(20)
    view.highlight(20)
    assert view.lines.item(20).font().bold() and not view.lines.item(19).font().bold()
    assert view._scroll.state() == QAbstractAnimation.Running
    music.is_playing.return_value = False
    view.sync_transport()
    assert view._scroll.state() == QAbstractAnimation.Paused
    before = view.lines.verticalScrollBar().value()
    QTest.qWait(220)
    assert view.lines.verticalScrollBar().value() == before
    p.btn_playing_lyrics.click()
    assert view._scroll.state() == QAbstractAnimation.Stopped and not errors


def test_page_fade_accepts_search_input_and_rapid_reversal_keeps_live_state(panel, monkeypatch):
    p, music, errors = panel
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)
    p.resize(850, 650)
    p.show()
    p.tabs.setCurrentIndex(1)
    p._on_search_done({"fixture": [OnlineTrack("fixture", str(i), f"歌曲 {i}") for i in range(60)]})
    p.online_stack.setCurrentWidget(p.results_page)
    listing = p.list_results
    listing.setCurrentRow(8)
    QTest.qWait(20)
    bar = listing.verticalScrollBar()
    bar.setValue(153)
    stack = p._content_pages
    button = p.btn_playing_lyrics
    original_geometry = button.geometry()
    QTest.mouseClick(button, Qt.LeftButton)
    assert stack.currentWidget() is p.playing_lyrics and stack._outgoing.isVisible()
    QTest.mouseClick(button, Qt.LeftButton)
    assert stack.currentWidget() is p._music_content and stack._outgoing.isHidden()
    assert listing.currentRow() == 8 and bar.value() == 153
    QTest.mouseClick(button, Qt.LeftButton)
    wait_until(stack._outgoing.isHidden)
    p.playing_lyrics.btn_close.click()
    assert stack._outgoing.isVisible() and stack.currentWidget() is p._music_content
    where = p.edit_kw.mapTo(stack, p.edit_kw.rect().center())
    hit = stack.childAt(where)
    assert hit is p.edit_kw or p.edit_kw.isAncestorOf(hit)
    QTest.mouseClick(p.edit_kw, Qt.LeftButton)
    QTest.keyClicks(p.edit_kw, "next search")
    assert p.edit_kw.text() == "next search"
    assert listing.currentRow() == 8 and bar.value() == 153
    assert button.geometry() == original_geometry and not errors
    p.action_download_tasks.trigger()
    assert stack._outgoing.isHidden() and p._download_pane.isVisible()


def test_reduced_motion_setting_updates_live_and_persists_in_isolated_config(panel, monkeypatch, tmp_path):
    from app.ui import settings_store

    p, music, errors = panel
    music.is_playing = Mock(return_value=True)
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)
    p.resize(850, 650)
    p.show()
    p.btn_playing_lyrics.click()
    assert p._content_pages._outgoing.isVisible()
    p._build_lyrics_menu()
    p.lyrics_actions["减少歌词动效"].trigger()
    assert settings_store.load_lyrics_prefs()["reduce_motion"] is True
    assert p.playing_lyrics.motion_reduced() and p._content_pages._outgoing.isHidden()
    engine, view = p._lyrics_window, p.playing_lyrics
    engine.set_lyrics([(i, f"第 {i} 行") for i in range(30)], "样本")
    view.highlight(22)
    assert view.active_line == 22 and view._scroll.state() == QAbstractAnimation.Stopped
    p.btn_playing_lyrics.click()
    p.btn_playing_lyrics.click()
    assert view.isVisible() and p._content_pages._outgoing.isHidden() and not errors


def test_confirmed_pause_holds_the_line_until_the_position_snapshot_catches_up(panel):
    p, music, errors = panel
    p.show()
    p.btn_playing_lyrics.click()
    engine = p._lyrics_window
    engine._timer.stop()
    engine.set_lyrics([(0, "当前句"), (1, "下一句")], "样本")
    music.is_playing = Mock(return_value=False)
    music._paused_snapshot = True
    music._playback_time_cache = (0.5, 0, False)
    music.current_playback_time = 1.02
    engine._sync()
    assert p.playing_lyrics.active_line == 0
    # A real position update, including a seek while paused, still applies.
    music._playback_time_cache = (1.02, 0, True)
    engine._sync()
    assert p.playing_lyrics.active_line == 1 and not errors
