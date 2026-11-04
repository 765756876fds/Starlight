"""Real Qt events render the real controller/queue, with silent playback fakes."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt
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
