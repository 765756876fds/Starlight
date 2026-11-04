"""Real Qt events render the real controller/queue, with silent playback fakes."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget

from app.core.command_bus import CommandBus
from app.core.event_bus import EventBus
from app.core.state_store import StateNamespace, StateStore
from app.music.controller import MusicController
from app.music.queue import PlayMode
from app.ui.panels.music_panel import MusicPanel
from app.ui.widgets.music_queue_dialog import MusicQueueDialog

pytestmark = pytest.mark.ui


def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "queue UI did not reach expected state"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def setup_view(qapp, tmp_path, monkeypatch):
    bus, commands, store = EventBus(), CommandBus(), StateStore()
    for module in ("app.music.controller", "app.ui.panels.music_panel"):
        monkeypatch.setattr(module + ".get_event_bus", lambda: bus)
        monkeypatch.setattr(module + ".get_command_bus", lambda: commands)
    monkeypatch.setattr("app.music.controller.get_state_store", lambda: store)
    lx = Mock()
    lx.is_connected.return_value = False
    monkeypatch.setattr("app.music.controller.LxMusicClient", lambda: lx)
    monkeypatch.setattr("app.ui.settings_store.save_settings_yaml", Mock())
    mpv = Mock()
    mpv._properties = {}
    mpv.loadfile.return_value = {"error": "success"}
    mpv.get_volume.return_value = 70
    c = MusicController({"database": str(tmp_path / "music.db"), "scan_dirs": []}, mpv_adapter=mpv)
    c.set_movie_pauser(lambda: True)
    c._running = True
    c._confirm_playback = Mock()  # no decoder or delayed bad-file checks in these UI tests
    songs = []
    for title in "ABCDE":
        path = tmp_path / (title + ".mp3")
        path.write_bytes(b"silent-test-placeholder")
        songs.append({"id": len(songs) + 1, "title": title, "artist": "测试歌手", "path": str(path)})
    c.queue.add_all(songs[:3])
    c.queue.play_at(0)
    c._current_song = songs[0]
    c._last_idle = False  # running media, not the stopped/idle sentinel
    c._update_state(playing=True)
    # The old unrelated progress poll is not needed to test event-driven queue updates.
    monkeypatch.setattr(MusicPanel, "_start_progress_poll", lambda self: None)
    panel = MusicPanel(c)
    panel.show()
    panel._timer.stop()
    mpv.reset_mock()
    yield SimpleNamespace(panel=panel, c=c, bus=bus, songs=songs, mpv=mpv)
    panel._timer.stop()
    panel.close()
    popover = panel.btn_volume._popover
    if popover:
        popover.removeEventFilter(panel.btn_volume)
        popover.close()
        popover.deleteLater()
    panel.deleteLater()
    bus.shutdown()
    c.stop()
    c.db.close()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def open_queue(s):
    QTest.mouseClick(s.panel.btn_queue, Qt.LeftButton)
    wait_for(lambda: s.panel._queue_dialog is not None and
             s.panel._queue_dialog.other_model.rowCount() == s.c.queue.size())
    return s.panel._queue_dialog


def titles(model):
    return [row["title"] for row in model.rows]


def test_entry_opens_nonmodal_reusable_view_without_playback_commands(setup_view):
    s = setup_view
    before = s.c.get_queue_snapshot()
    dialog = open_queue(s)
    assert not dialog.isModal()
    assert "正在播放 · A" in dialog.current_label.text()
    assert s.panel.tabs.count() == 5  # existing music subpages unchanged
    dialog.close()
    QTest.mouseClick(s.panel.btn_queue, Qt.LeftButton)
    wait_for(dialog.isVisible)
    assert s.panel._queue_dialog is dialog
    assert s.c.get_queue_snapshot() == before
    assert s.mpv.mock_calls == []


@pytest.mark.parametrize("automatic", [False, True])
def test_real_manual_and_eof_consumption_updates_view_in_priority_order(setup_view, automatic):
    s = setup_view
    dialog = open_queue(s)
    s.c.set_play_mode(PlayMode.SHUFFLE)
    s.c.add_next(s.songs[3])
    s.c.add_next(s.songs[4])
    wait_for(lambda: titles(dialog.next_model) == list("ED"))
    assert "随机播放" in dialog.mode_label.text()
    assert "不代表随机播放先后" in dialog.notice.text()
    # These are the production manual next / idle-confirmed automatic-next entries.
    s.mpv.get_property.return_value = True
    (s.c._auto_next if automatic else s.c.next)()
    wait_for(lambda: "正在播放 · E" in dialog.current_label.text())
    assert titles(dialog.next_model) == ["D"]
    s.mpv.loadfile.assert_called_once_with(s.songs[4]["path"])
    s.c._last_next_ts = 0  # model a later keypress / later track completion
    (s.c._auto_next if automatic else s.c.next)()
    wait_for(lambda: "正在播放 · D" in dialog.current_label.text())
    assert titles(dialog.next_model) == []
    assert s.mpv.loadfile.call_args.args == (s.songs[3]["path"],)


def test_worker_events_render_only_on_gui_thread_and_reopen_does_not_duplicate_connections(setup_view, monkeypatch):
    s = setup_view
    dialog = open_queue(s)
    calls = []
    original = dialog.set_snapshot

    def render(snapshot):
        calls.append(QThread.currentThread())
        original(snapshot)

    monkeypatch.setattr(dialog, "set_snapshot", render)
    for _ in range(3):
        dialog.close()
        QTest.mouseClick(s.panel.btn_queue, Qt.LeftButton)
        wait_for(lambda: not s.panel._queue_refresh_timer.isActive())
    calls.clear()
    worker = threading.Thread(target=lambda: s.c.add_next(s.songs[3]))
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    wait_for(lambda: titles(dialog.next_model) == ["D"])
    assert calls == [QApplication.instance().thread()]
    assert len(s.panel.findChildren(MusicQueueDialog)) == 1


def test_mode_pause_resume_stop_remove_and_clear_update_without_polling(setup_view):
    s = setup_view
    dialog = open_queue(s)
    s.c.set_play_mode(PlayMode.REPEAT_ONE)
    wait_for(lambda: "单曲循环" in dialog.mode_label.text())
    assert "单曲循环当前歌曲" in dialog.notice.text()
    s.c.add_next(s.songs[3])
    wait_for(lambda: titles(dialog.next_model) == ["D"])
    assert "循环最后播到" in dialog.notice.text()
    s.c.pause()
    wait_for(lambda: "已暂停" in dialog.current_label.text())
    s.c.resume()
    wait_for(lambda: "正在播放" in dialog.current_label.text())
    s.c.queue.remove(1)  # remove requested D using existing API, no new UI action
    wait_for(lambda: not dialog.next_model.rows)
    assert titles(dialog.other_model) == list("ABC")
    s.c.clear_queue()
    wait_for(lambda: not dialog.other_model.rows)
    assert "已停止" in dialog.current_label.text()
    assert "播放队列为空" in dialog.empty_label.text()


def test_random_queue_without_requests_does_not_claim_a_definite_next(setup_view):
    s = setup_view
    s.c.set_play_mode(PlayMode.SHUFFLE)
    dialog = open_queue(s)
    assert not dialog.next_model.rows
    assert "后续随机选择" in dialog.notice.text()
    assert "不代表随机播放先后" in dialog.notice.text()


def test_online_and_local_labels_and_external_current_are_accurate(setup_view):
    s = setup_view
    online = {"title": "在线歌曲", "artist": "网络歌手", "source_type": "online",
              "source_name": "测试音源", "track_id": "42"}
    s.c.queue.add(online)
    dialog = open_queue(s)
    texts = [dialog.other_model.data(dialog.other_model.index(i)) for i in range(4)]
    assert "本地" in texts[0]
    assert "在线 · 测试音源" in texts[-1] and "网络歌手" in texts[-1]
    s.c._current_song = dict(online, source="online", path="https://example.invalid/audio")
    s.c._update_state(playing=True)
    wait_for(lambda: "在线歌曲" in dialog.current_label.text())
    assert dialog.other_model.current_index is None  # cursor still A; no false current row


def test_online_stream_start_overrides_previous_paused_snapshot(setup_view):
    from app.music.online.models import OnlineTrack
    s = setup_view
    dialog = open_queue(s)
    online = {"title": "在线歌曲", "artist": "歌手", "source_type": "online",
              "source_name": "测试音源", "track_id": "42"}
    s.c.queue.add(online)
    s.c.queue.play_at(3)
    s.c.pause()
    wait_for(lambda: "已暂停" in dialog.current_label.text())
    track = OnlineTrack(source_name="测试音源", track_id="42", title="在线歌曲", artist="歌手")
    with s.c._lock:
        result = s.c._apply_stream(track, "https://example.invalid/audio", None)
    assert result is not None
    wait_for(lambda: "正在播放 · 在线歌曲" in dialog.current_label.text())
    assert dialog.other_model.current_index == 3


def test_returning_to_music_page_keeps_same_queue_cursor_and_dialog(setup_view, qapp):
    s = setup_view
    stack = QStackedWidget()
    stack.addWidget(s.panel)
    stack.addWidget(QWidget())
    stack.show()
    s.panel._timer.stop()
    dialog = open_queue(s)
    before = s.c.get_queue_snapshot()
    stack.setCurrentIndex(1)
    assert not dialog.isVisible()
    stack.setCurrentIndex(0)
    s.panel._timer.stop()
    QTest.mouseClick(s.panel.btn_queue, Qt.LeftButton)
    wait_for(dialog.isVisible)
    assert s.panel._queue_dialog is dialog
    assert s.c.get_queue_snapshot() == before
    # Return ownership to fixture; closing the temporary stack must not destroy panel.
    s.panel.setParent(None)
    stack.close()
    stack.deleteLater()
    qapp.processEvents()


def test_busy_playback_is_bounded_and_recovers_on_next_state_event(setup_view):
    s = setup_view
    with s.c._lock:
        QTest.mouseClick(s.panel.btn_queue, Qt.LeftButton)
        dialog = s.panel._queue_dialog
        wait_for(lambda: "暂时无法读取" in dialog.notice.text(), timeout=2)
        assert not s.panel._queue_refresh_timer.isActive()
    s.c._update_state(playing=True)
    wait_for(lambda: "正在播放 · A" in dialog.current_label.text())
    assert titles(dialog.other_model) == list("ABC")


def test_closed_view_does_not_query_on_events_and_reopens_with_latest_state(setup_view, monkeypatch):
    s = setup_view
    dialog = open_queue(s)
    dialog.close()
    query = Mock(wraps=s.c.get_queue_snapshot)
    monkeypatch.setattr(s.c, "get_queue_snapshot", query)
    s.c.queue.clear()
    QTest.qWait(100)
    assert query.call_count == 0
    QTest.mouseClick(s.panel.btn_queue, Qt.LeftButton)
    wait_for(lambda: query.called)
    assert dialog.other_model.rowCount() == 0
    assert "当前歌曲未加入队列" in dialog.empty_label.text()


def test_long_queue_uses_scrollable_model_and_does_not_create_row_widgets(setup_view):
    s = setup_view
    s.c.queue.add_all([{"title": f"歌曲 {i}", "artist": "歌手"} for i in range(5000)])
    dialog = open_queue(s)
    wait_for(lambda: dialog.other_list.verticalScrollBar().maximum() > 0)
    assert dialog.other_model.rowCount() == 5003
    assert len(dialog.other_list.findChildren(QWidget)) < 20
    dialog.other_list.scrollToBottom()
    wait_for(lambda: dialog.other_list.verticalScrollBar().value() > 0)
    last = dialog.other_model.index(5002)
    assert "歌曲 4999" in dialog.other_model.data(last)


@pytest.mark.parametrize("online", [False, True])
def test_search_context_menu_appends_without_playing_or_changing_playlist(setup_view, monkeypatch, online):
    from PySide6.QtWidgets import QMenu
    from app.music.online.models import OnlineTrack
    s = setup_view
    menus = []

    def choose_append(menu, pos):
        menus.append([a.text() for a in menu.actions()])
        next(a for a in menu.actions() if a.text() == "添加到播放队列").trigger()

    monkeypatch.setattr(QMenu, "exec_", choose_append)
    if online:
        track = OnlineTrack(source_name="测试音源", track_id="42", title="在线歌曲", artist="歌手",
                            extra={"_raw": {"id": "raw-42"}})
        s.panel.tabs.setCurrentIndex(1)
        s.panel._on_search_done({"测试音源": [track]})
        view = s.panel.list_results
    else:
        monkeypatch.setattr(s.c, "search_local", lambda *args, **kwargs: [s.songs[3]])
        s.panel._refresh_local_list("D")
        view = s.panel.list_local
    wait_for(lambda: not view.visualItemRect(view.item(0)).isEmpty())
    view.customContextMenuRequested.emit(view.visualItemRect(view.item(0)).center())
    assert len(menus) == 1 and "添加到歌单 ▸" in menus[0] and "下一首播放" in menus[0]
    assert s.c.queue.size() == 4
    assert s.c._current_song is s.songs[0]
    added = s.c.queue.get_queue()[-1]
    if online:
        assert added["source_type"] == "online" and added["track_id"] == "42"
        assert added["extra"]["_raw"] == {"id": "raw-42"}
    else:
        assert added["path"] == s.songs[3]["path"] and added["id"] == s.songs[3]["id"]
    assert "已添加到播放队列" in s.panel.lbl_status.text()
    assert s.c.get_playlists() == []
    assert s.mpv.mock_calls == []


def test_remove_menu_targets_priority_index_not_display_row(setup_view):
    s = setup_view
    s.c.add_next(s.songs[3])
    s.c.add_next(s.songs[4])
    QTest.mouseClick(s.panel.btn_queue, Qt.LeftButton)
    dialog = s.panel._queue_dialog
    wait_for(lambda: titles(dialog.next_model) == list("ED"))
    assert dialog._build_remove_menu(dialog.other_model, 0) is None  # current A
    menu = dialog._build_remove_menu(dialog.next_model, 0)  # visual row 0 is underlying E at 1
    menu.actions()[0].trigger()
    wait_for(lambda: titles(dialog.next_model) == ["D"])
    assert s.c.queue.get_current() is s.songs[0]
    assert [song["title"] for song in s.c.queue.get_queue()] == list("ADBC")
    assert "已移出播放队列" in dialog.action_status.text()
    assert s.mpv.mock_calls == []


def test_duplicate_remove_menu_keeps_exact_other_occurrence_and_file(setup_view):
    from pathlib import Path
    s = setup_view
    s.c.add_to_queue(s.songs[3])
    s.c.add_to_queue(s.songs[3])
    first, second = s.c.queue.get_queue()[-2:]
    dialog = open_queue(s)
    menu = dialog._build_remove_menu(dialog.other_model, 4)
    menu.actions()[0].trigger()
    wait_for(lambda: dialog.other_model.rowCount() == 4)
    assert s.c.queue.get_queue()[-1] is first
    assert not any(item is second for item in s.c.queue.get_queue())
    assert Path(s.songs[3]["path"]).read_bytes() == b"silent-test-placeholder"
    assert s.c.get_playlists() == []


def test_old_menu_after_refresh_cannot_remove_different_song(setup_view):
    s = setup_view
    dialog = open_queue(s)
    menu = dialog._build_remove_menu(dialog.other_model, 1)  # B
    s.c.add_next(s.songs[3])  # now underlying index 1 is D
    wait_for(lambda: titles(dialog.next_model) == ["D"])
    menu.actions()[0].trigger()
    wait_for(lambda: "未移出" in dialog.action_status.text())
    assert [song["title"] for song in s.c.queue.get_queue()] == list("ADBC")


def test_song_becomes_current_while_menu_open_cannot_be_removed(setup_view):
    s = setup_view
    dialog = open_queue(s)
    menu = dialog._build_remove_menu(dialog.other_model, 1)
    s.c.next()  # B starts playing, structure unchanged
    wait_for(lambda: "正在播放 · B" in dialog.current_label.text())
    menu.actions()[0].trigger()
    wait_for(lambda: "未移出" in dialog.action_status.text())
    assert s.c.queue.get_current()["title"] == "B"
    assert titles(dialog.other_model) == list("ABC")
    assert dialog._build_remove_menu(dialog.other_model, 1) is None


@pytest.mark.parametrize("result,expected", [(True, "已加入播放队列"), (False, "未成功"),
                                            (None, "无法确认"), ("unexpected", "无法确认"),
                                            (RuntimeError("test error"), "未成功")])
def test_append_result_feedback_is_strict(setup_view, monkeypatch, result, expected):
    s = setup_view
    method = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setattr(s.c, "add_to_queue", method)
    menu = s.panel._build_song_menu(s.songs[3])
    next(a for a in menu.actions() if a.text() == "添加到播放队列").trigger()
    assert expected in s.panel.lbl_status.text()
    assert method.call_count == 1


@pytest.mark.parametrize("result,expected", [(True, "已移出播放队列"), (False, "未成功"),
                                            (None, "无法确认"), ("unexpected", "无法确认"),
                                            (RuntimeError("test error"), "未成功")])
def test_remove_feedback_and_execution_stay_on_gui_thread(setup_view, monkeypatch, result, expected):
    s = setup_view
    dialog = open_queue(s)
    threads = []

    def remove(index, revision):
        threads.append(QThread.currentThread())
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(s.c, "remove_from_queue", remove)
    index, revision = dialog.other_model.rows[1]["index"], dialog._revision
    worker = threading.Thread(target=lambda: dialog.remove_requested.emit(index, revision))
    worker.start()
    worker.join(timeout=1)
    wait_for(lambda: expected in dialog.action_status.text())
    assert threads == [QApplication.instance().thread()]


def test_edited_queue_survives_page_switch_and_dialog_reopen_without_duplicate_actions(setup_view, qapp, monkeypatch):
    s = setup_view
    s.c.add_to_queue(s.songs[3])
    stack = QStackedWidget()
    stack.addWidget(s.panel)
    stack.addWidget(QWidget())
    stack.show()
    s.panel._timer.stop()
    dialog = open_queue(s)
    for _ in range(3):
        stack.setCurrentIndex(1)
        stack.setCurrentIndex(0)
        s.panel._timer.stop()
        QTest.mouseClick(s.panel.btn_queue, Qt.LeftButton)
    wait_for(lambda: dialog.other_model.rowCount() == 4)
    method = Mock(wraps=s.c.remove_from_queue)
    monkeypatch.setattr(s.c, "remove_from_queue", method)
    dialog._build_remove_menu(dialog.other_model, 3).actions()[0].trigger()
    wait_for(lambda: dialog.other_model.rowCount() == 3)
    assert method.call_count == 1
    assert titles(dialog.other_model) == list("ABC")
    assert s.c._current_song is s.songs[0]
    s.panel.setParent(None)
    stack.close()
    stack.deleteLater()
    qapp.processEvents()


def test_queue_list_context_signal_removes_only_clicked_waiting_row(setup_view):
    from PySide6.QtWidgets import QMenu
    s = setup_view
    dialog = open_queue(s)
    chosen = []

    def choose_remove():
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            chosen.append(menu.actions()[0].text())
            menu.actions()[0].trigger()
            menu.close()

    # Exercise the real native QMenu event loop, with a bounded escape if selection fails.
    escape = QTimer(dialog)
    escape.setSingleShot(True)
    escape.timeout.connect(lambda: [menu.close() for menu in dialog.findChildren(QMenu)])
    index = dialog.other_model.index(1)
    wait_for(lambda: not dialog.other_list.visualRect(index).isEmpty())
    escape.start(1000)
    QTimer.singleShot(0, choose_remove)
    dialog.other_list.customContextMenuRequested.emit(dialog.other_list.visualRect(index).center())
    escape.stop()
    assert chosen == ["移出播放队列"]
    wait_for(lambda: titles(dialog.other_model) == list("AC"))
    assert s.c._current_song is s.songs[0]


def test_missing_local_song_and_busy_remove_give_actionable_failure(setup_view):
    s = setup_view
    s.panel._on_append_queue({"title": "无路径歌曲", "music_id": 0})
    assert "未添加到播放队列" in s.panel.lbl_status.text()
    assert s.c.queue.size() == 3
    dialog = open_queue(s)
    menu = dialog._build_remove_menu(dialog.other_model, 1)
    with s.c._lock:
        menu.actions()[0].trigger()
        wait_for(lambda: "稍后重试" in dialog.action_status.text())
    assert [song["title"] for song in s.c.queue.get_queue()] == list("ABC")
