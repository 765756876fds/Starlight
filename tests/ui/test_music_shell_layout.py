import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListView
from tests.ui.test_music_like_button import panel as panel

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("width", [760, 1100])
def test_overlay_leaves_song_grid_geometry_and_selection_intact(panel, width):
    widget, controller, _ = panel
    widget.enable_shell_navigation()
    widget.resize(width, 650)
    widget.tabs.setCurrentIndex(0)
    QApplication.processEvents()
    assert widget.list_local.viewMode() == QListView.ListMode
    width_before = widget.list_local.viewport().width()
    widget._show_download_tasks()
    QApplication.processEvents()
    assert widget._media_pane.isVisible() and widget._media_pane.width() <= 380
    assert widget.list_local.viewport().width() == width_before
    widget._show_queue()
    QApplication.processEvents()
    assert widget._queue_dialog.isVisible() and not widget._queue_dialog.isWindow()
    assert widget.list_local.viewport().width() == width_before
    widget._close_media_pane(animate=False)
    assert widget.tabs.currentIndex() == 0


def test_sort_keeps_selected_recording_and_local_context(panel):
    widget, _, _ = panel
    widget.enable_shell_navigation()
    widget.tabs.setCurrentIndex(0)
    songs = [dict(id=1,title='C',artist='A'),dict(id=2,title='B',artist='A'),dict(id=3,title='A',artist='B')]
    widget._show_lyric_results(songs,'')
    widget.list_local.setCurrentRow(0)
    widget._sort_tracks('title')
    assert [song['id'] for song in widget._local_songs] == [3,2,1]
    assert widget.list_local.currentItem().data(Qt.UserRole)['id'] == 1
    assert widget.list_local.currentRow() == 2


def test_local_quality_menu_only_changes_future_streaming(panel, tmp_path, monkeypatch):
    from app.ui import settings_store
    widget, controller, _ = panel
    monkeypatch.setattr(settings_store,'UI_PREFS_PATH',str(tmp_path/'prefs.json'))
    assert settings_store.save_download_prefs({'quality':'standard','lyrics':False})
    prior = settings_store.load_download_prefs()
    controller._current_song = dict(id=7,title='Local',path='chosen.flac')
    widget.quality_button.select('hires')
    assert controller.playback_quality.preference == 'hires'
    assert settings_store.load_ui_prefs()['playback_quality']=='hires'
    assert settings_store.load_download_prefs()==prior
    assert not widget.quality_button.switch._running


def test_up_next_uses_selected_occurrence_and_rejects_a_stale_menu(panel):
    _, controller, _ = panel
    a, b, c = [dict(id=i, title=str(i),path=f"{i}.flac") for i in range(3)]
    controller.queue.replace_context([a,b,c],0)
    controller._current_song = a
    revision = controller.queue.snapshot(a)["revision"]
    assert controller.replay_queue_entry_next(2,revision)
    assert not controller.replay_queue_entry_next(1,revision)
    assert controller.queue.next() == c
    assert controller.queue.next() == b


def test_row_favorite_uses_selected_recording_without_starting_playback(panel):
    from tests.ui.test_music_like_button import set_song, wait_for
    widget, controller, mpv = panel
    current = dict(id=1, title="当前")
    selected = dict(source_type="online", source_name="A", track_id="studio", title="选择的录音")
    set_song(controller, current)
    wait_for(widget.btn_like.isEnabled)
    menu = widget._build_song_menu(selected)
    action = next(a for a in menu.actions() if a.text() == "收藏到我喜欢的音乐")
    action.trigger()
    wait_for(lambda: not widget._row_favorites)
    assert controller.is_liked_song(selected) is True
    assert controller.is_liked_song(current) is False
    assert controller.get_current_song() is current
    assert not widget.btn_like.isChecked()
    mpv.loadfile.assert_not_called()


def test_quality_menu_opens_above_the_footer_when_space_permits(panel):
    from PySide6.QtCore import QPoint
    widget, _, _ = panel
    widget.resize(850, 650)
    widget.move(0, 0)
    QApplication.processEvents()
    button = widget.quality_button
    anchor = button.mapToGlobal(QPoint())
    menu = button.menu()
    menu.popup(anchor)
    QApplication.processEvents()
    try:
        assert anchor.y() - menu.height() > button.screen().availableGeometry().top()
        assert menu.geometry().bottom() < anchor.y()
        assert menu.isVisible()
    finally:
        menu.close()


def test_playlist_table_gets_album_and_duration_from_exact_local_recording(panel):
    from tests.ui.test_music_like_button import wait_for
    widget, controller, _ = panel
    original = dict(title="同名歌", artist="歌手", album="录音室", duration=64.0,
                    path="studio.flac", filename="studio.flac")
    live = dict(original, album="现场", duration=99.0, path="live.flac", filename="live.flac")
    controller.db.insert_song(live)
    original["music_id"] = controller.db.insert_song(original)
    playlist = controller.create_playlist("录音室歌单")
    assert controller.add_to_playlist(playlist, original)
    controller.db.add_history(original["title"], original["artist"], "local", music_id=original["music_id"])
    widget.tabs.setCurrentIndex(3)
    widget._refresh_playlists(preferred_id=playlist)
    wait_for(lambda: widget.list_playlist_songs.count() == 1)
    row = widget.list_playlist_songs.item(0).data(Qt.UserRole)
    assert (row["album"], row["duration"]) == ("录音室", 64.0)
    assert controller.get_history()[0]["album"] == "录音室"


def test_empty_favorites_never_displays_an_unrelated_playlist(panel):
    from tests.ui.test_music_like_button import wait_for
    widget, controller, _ = panel
    item = dict(id=1, title="只属于普通歌单")
    pid = controller.create_playlist("普通歌单")
    assert controller.add_to_playlist(pid, item)
    widget.enable_shell_navigation()
    widget.tabs.setCurrentIndex(3)
    wait_for(lambda: widget.list_playlist_songs.count() == 1)
    widget.set_favorites_view(True)
    wait_for(lambda: not widget._playlist_task._running)
    assert widget.list_playlist_songs.count() == 0
    assert not widget.play_playlist_button.isEnabled()
    assert "尚未收藏" in widget.lbl_playlist_status.text()
    assert widget.heading.text() == "我的收藏"
    assert controller.set_liked_song(dict(id=2, title="刚收藏的歌曲"), True)
    widget._refresh_visible_playlists()
    wait_for(lambda: widget.list_playlist_songs.count() == 1)
    assert widget.list_playlist_songs.item(0).data(Qt.UserRole)["title"] == "刚收藏的歌曲"
    widget.set_favorites_view(False)
    wait_for(lambda: widget.playlist_title.text() == "普通歌单")
    assert widget.list_playlist_songs.item(0).data(Qt.UserRole)["title"] == item["title"]


def test_transport_drawer_escape_restores_focus_without_reflow(panel):
    from PySide6.QtTest import QTest
    widget, _, _ = panel
    widget.enable_shell_navigation()
    widget.resize(760, 650)
    widget.tabs.setCurrentIndex(0)
    widget.activateWindow()
    widget.btn_queue.setFocus()
    QApplication.processEvents()
    original = widget.list_local.viewport().geometry()
    widget._show_queue()
    QApplication.processEvents()
    close = widget._queue_dialog.btn_close
    assert close.hasFocus()
    assert widget._media_pane.geometry().top() > 0
    QTest.keyClick(close, Qt.Key_Escape)
    assert widget._media_pane.isHidden()
    assert widget.btn_queue.hasFocus()
    assert widget.list_local.viewport().geometry() == original


def test_voice_bar_expands_only_for_feedback_or_relevant_fault(monkeypatch):
    from types import SimpleNamespace
    from app.core.notifications import NotificationCenter
    from app.ui.panels.status_bar import StatusBar
    from PySide6.QtWidgets import QMainWindow

    center = NotificationCenter()
    monkeypatch.setattr("app.ui.panels.status_bar.get_notification_center", lambda: center)
    state = SimpleNamespace(on=False, ready=False)
    voice = SimpleNamespace(is_mic_on=lambda: state.on, is_listening=lambda: state.ready)
    window = QMainWindow()
    bar = StatusBar({"voice": voice, "asr": SimpleNamespace(is_available=lambda: state.ready)})
    window.setStatusBar(bar)
    window.resize(850, 630)
    window.show()
    try:
        assert bar.island.isHidden() and bar.lbl_asr.isHidden()
        assert not bar.btn_mic.isHidden()
        state.on = True
        bar.refresh()
        assert bar.lbl_asr.isVisible()
        state.ready = True
        bar.refresh()
        assert bar.lbl_asr.isHidden() and bar.btn_mic.text() == "正在聆听"
        assert bar.island.isHidden()
        center.post("语音未识别", source="voice", phase="done", status="warning")
        bar.refresh()
        assert bar.island.isVisible() and bar.minimumHeight() >= 44
        bar.island.collapse()
        from PySide6.QtTest import QTest
        QTest.qWait(220)
        assert bar.island.isHidden() and bar.minimumHeight() < 44
    finally:
        bar._timer.stop()
        bar._notice_stream.dispose()
        window.close()


def test_sparse_results_keep_actions_near_tracks_and_dense_results_expand(panel):
    from PySide6.QtWidgets import QListWidgetItem
    widget, _, _ = panel
    widget.enable_shell_navigation()
    listing = widget.list_results
    listing.addItem(QListWidgetItem("一首歌"))
    widget._fit_sparse_results()
    assert listing.maximumHeight() == listing.sizeHintForRow(0)
    assert widget.results_page.layout().stretch(widget.results_page.layout().count() - 1) == 1
    listing.addItems(["第二首", "第三首"])
    widget._fit_sparse_results()
    assert listing.maximumHeight() > 10000
