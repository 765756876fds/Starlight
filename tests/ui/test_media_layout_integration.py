"""Qt integration of paired media screens with isolated controllers and media."""
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStackedWidget

from app.music.online.models import OnlineTrack
from app.ui.panels.movie_panel import _MovieDetailDialog
from app.ui.theme.manager import get_theme_manager
from tests.ui.test_movie_voice_window import scene
from tests.ui.test_music_search_reliability import panel, qapp


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_narrow_workspace_keeps_music_browsable_with_either_pane_and_across_switches(panel, scene, qapp, mode):
    p, music, errors = panel
    f = scene.panel
    theme = get_theme_manager(qapp)
    old = theme.mode
    theme.set_theme(mode, persist=False)
    callbacks = []
    music.get_playlists = lambda: []
    music.download_song = lambda song, cb, **kw: callbacks.append(cb) or True
    host = QStackedWidget()
    host.addWidget(p)
    host.addWidget(f)
    host.resize(850, 680)
    host.show()
    try:
        p._timer.stop()
        p.tabs.setCurrentIndex(1)
        p._on_search_done({"fixture": [OnlineTrack("fake", "1", "星桥", "纸鸢", "夜航")]})
        p.online_stack.setCurrentWidget(p.results_page)
        QTest.mouseClick(p.btn_playing_lyrics, Qt.LeftButton)
        assert not p.tabs.isVisible() and not p.list_results.isVisible()
        assert p._content_pages.currentWidget() is p.playing_lyrics
        assert p.list_results.count() == 1
        assert p.playing_lyrics.isVisible() and p.btn_queue.isVisible() and p.btn_like.isVisible()
        assert p.playing_lyrics.width() == p._content_pages.width()
        assert p.playing_lyrics.mapTo(p, p.playing_lyrics.rect().bottomLeft()).y() < p.btn_playing_lyrics.mapTo(p, p.btn_playing_lyrics.rect().topLeft()).y()
        assert host.width() == 850
        engine = p._lyrics_window
        engine.set_lyrics([(0, "第一句"), (1, "第二句")], "星桥")
        music.current_playback_time = 1.1
        engine._sync()
        assert p.playing_lyrics.active_line == 1
        p.action_download_tasks.trigger()
        tasks = p._download_pane
        assert tasks.isVisible() and p.list_results.isVisible()
        assert p.tabs.currentIndex() == 1 and p.online_stack.currentWidget() is p.results_page
        assert not p.btn_playing_lyrics.isChecked() and not engine._started
        p._download_result(0)
        for _ in range(3):
            host.setCurrentWidget(f)
            assert not tasks.isVisible()
            host.setCurrentWidget(p)
            assert tasks.isVisible() and p.list_results.count() == 1
        callbacks[0]["on_progress"]("", 43)
        assert "43%" in tasks.items.item(0).text()
        task = tasks.items.item(0)
        tasks.btn_close.click()
        assert p._media_pane.isHidden()
        callbacks[0]["on_fail"]("", "音频未能完整下载")
        assert p._media_pane.isHidden()
        p.action_download_tasks.trigger()
        assert p._download_pane is tasks and tasks.items.item(0) is task
        assert tasks.retry.isVisible() and not tasks.open_folder.isVisible()
        assert not errors
        scene.mpv.loadfile.assert_not_called()
    finally:
        host.removeWidget(p)
        host.removeWidget(f)
        p.setParent(None)
        f.setParent(None)
        host.close()
        theme.set_theme(old, persist=False)


def test_film_filters_and_details_are_contextual_and_pending_click_is_cancelled(scene):
    f = scene.panel
    f.resize(850, 680)
    f.show()
    f._timer.stop()
    assert f.search_options.isHidden() and f.btn_home.isHidden()
    assert f._now_readout.isHidden()
    f.stack_results.setCurrentWidget(f.list_posters)
    f._on_search_done([scene.item])
    assert f.search_options.isVisible() and f.search_filters.isHidden()
    f.search_options.click()
    f.combo_content_filter.setCurrentIndex(1)
    f.search_options.click()
    assert f.search_options.text() == "筛选（1）" and f.search_filters.isHidden()
    f.combo_content_filter.setCurrentIndex(0)
    assert f.search_options.text() == "筛选"
    item = f.list_posters.item(0)
    assert item is not None
    f._show_detail = Mock()
    f._on_item_clicked(item)
    assert f._click_timer.isActive()
    f.btn_home.click()
    assert not f._click_timer.isActive() and f._pending_click_item is None
    assert f.btn_home.isHidden() and f.search_options.isHidden()
    f._on_click_timeout()
    f._show_detail.assert_not_called()
    f._on_playback_status(scene.controller._playback_gen, "PENDING", "internal")
    assert f.lbl_status.text() == "正在加载…" and f._now_readout.isVisible()
    f._on_playback_status(scene.controller._playback_gen, "FAILED", "HTTP hidden")
    assert "HTTP" not in f.lbl_status.text() and f._now_readout.isHidden()
    scene.mpv.loadfile.assert_not_called()


def test_film_detail_keeps_source_optional_without_hiding_play(scene):
    f = scene.panel
    dlg = _MovieDetailDialog(dict(scene.item, _source="仅在展开后显示"), f)
    dlg.show()
    assert dlg.lbl_name.text() == scene.item["vod_name"]
    assert dlg.btn_play.isVisible() and dlg.btn_play.isEnabled()
    from PySide6.QtWidgets import QLabel
    source = next(label for label in dlg.findChildren(QLabel) if "仅在展开后显示" in label.text())
    assert not source.isVisible()
    dlg.btn_source_details.click()
    assert source.isVisible() and dlg.btn_play.isVisible()
    dlg.close()


def test_parent_destruction_releases_music_events(qapp, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QWidget
    from app.core.event_bus import Event, EventBus, EventType
    from app.ui.panels import music_panel

    bus = EventBus()
    monkeypatch.setattr(music_panel, "get_event_bus", lambda: bus)
    host = QWidget()
    panel = music_panel.MusicPanel(parent=host)
    delivered, errors = [], []
    panel._queue_changed.connect(lambda: delivered.append(True))
    bus.subscribe(EventType.SYSTEM_ERROR, errors.append)
    try:
        bus.publish(Event(EventType.MUSIC_STATE_CHANGED, {}))
        assert delivered == [True]
        host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        bus.publish(Event(EventType.MUSIC_STATE_CHANGED, {}))
        assert errors == []
        assert delivered == [True]
    finally:
        bus.shutdown()


def test_fast_tab_reversal_keeps_search_input_and_browse_position(qapp):
    from PySide6.QtWidgets import QLineEdit, QListWidget
    from app.ui.widgets.music_content_stack import MediaTabs

    tabs = MediaTabs()
    tabs.reduce_motion = lambda: False
    listing = QListWidget()
    listing.addItems([str(i) for i in range(160)])
    search = QLineEdit()
    tabs.addTab(listing, "曲库")
    tabs.addTab(search, "搜索")
    tabs.resize(850, 480)
    tabs.show()
    try:
        listing.setCurrentRow(84)
        listing.scrollToItem(listing.currentItem(), QListWidget.PositionAtCenter)
        qapp.processEvents()
        position = listing.verticalScrollBar().value()
        bar = tabs.tabBar()
        QTest.mouseClick(bar, Qt.LeftButton, pos=bar.tabRect(1).center())
        assert tabs.currentWidget() is search and tabs._outgoing.isVisible()
        assert tabs._outgoing.testAttribute(Qt.WA_TransparentForMouseEvents)
        search.setFocus()
        QTest.keyClicks(search, "moon")
        assert search.hasFocus() and search.text() == "moon"
        QTest.mouseClick(bar, Qt.LeftButton, pos=bar.tabRect(0).center())
        assert not tabs._outgoing.isVisible()
        assert listing.currentRow() == 84 and listing.verticalScrollBar().value() == position
        tabs.reduce_motion = lambda: True
        tabs.setCurrentWidget(search)
        assert search.text() == "moon" and not tabs._outgoing.isVisible()
    finally:
        tabs.close()


def test_drawer_reversal_is_immediate_and_low_power_skips_the_layer(panel, qapp):
    p, _, _ = panel
    view = p.playing_lyrics
    p.resize(850, 680)
    p.show()
    QTest.qWait(20)
    assert view.engine is None
    view.remember_motion_preferences({"reduce_motion": False, "stage_low_power": False})
    from app.ui.theme.motion import set_reduced_motion
    from app.ui.theme import motion
    old = motion._forced_reduced
    set_reduced_motion(False)
    try:
        p.action_download_tasks.trigger()
        assert p._media_pane.isVisible() and p._drawer_transition.isVisible()
        p._close_media_pane()
        assert p._media_pane.isHidden() and not p._drawer_transition.isVisible()
        for key in ("stage_low_power", "reduce_motion"):
            view.remember_motion_preferences({"stage_low_power": False, "reduce_motion": False, key: True})
            assert view.navigation_motion_reduced()
            p.action_download_tasks.trigger()
            assert p._media_pane.isVisible() and not p._drawer_transition.isVisible()
            p._close_media_pane()
            assert p._media_pane.isHidden() and not p._drawer_transition.isVisible()
    finally:
        set_reduced_motion(old)
        p.close()


def test_direct_global_navigation_preserves_input_without_a_snapshot(qapp, monkeypatch):
    from PySide6.QtWidgets import QLineEdit, QWidget
    from app.ui.widgets import music_content_stack

    tabs = music_content_stack.MediaTabs(animate_navigation=False)
    tabs.reduce_motion = lambda: False
    search = QLineEdit()
    tabs.addTab(search, "音乐")
    tabs.addTab(QWidget(), "电影")
    tabs.show()
    try:
        qapp.processEvents()
        def unexpected_snapshot(*args):
            raise AssertionError("Top-level navigation rendered the outgoing page")
        monkeypatch.setattr(music_content_stack, "_snapshot", unexpected_snapshot)
        for index in (1, 0, 1, 0):
            tabs.setCurrentIndex(index)
            assert tabs.currentIndex() == index and tabs._outgoing.isHidden()
        search.setFocus()
        QTest.keyClicks(search, "starlight")
        assert search.text() == "starlight"
    finally:
        tabs.close()
