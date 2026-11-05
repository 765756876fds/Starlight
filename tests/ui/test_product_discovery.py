import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QThread, Qt, QModelIndex
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QLabel

from app.core import notifications
from app.music.online.models import OnlineTrack
from app.movie.library import MovieLibrary
from app.ui.widgets.latest_task import LatestTask
from app.ui.widgets.music_discovery import MusicDiscovery
from app.ui.widgets.movie_home import MovieHome
from app.ui.widgets.discovery_cards import DISPLAY_ROLE
from app.ui.panels.game_workbench import GameWorkbench
from tests.ui.test_music_queue_view import setup_view


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    # main.py applies the global theme before constructing any business page.
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
    return app


def wait(predicate, timeout=1200):
    elapsed = 0
    while not predicate() and elapsed < timeout:
        QTest.qWait(10)
        elapsed += 10
    assert predicate(), "UI did not reach the expected result"


def test_only_latest_queued_read_updates_gui_and_the_first_read_is_not_cancelled(qapp):
    task = LatestTask()
    started, release = threading.Event(), threading.Event()
    calls, seen = [], []
    def slow():
        calls.append("A")
        started.set()
        assert release.wait(1)
        return "old"
    task.completed.connect(lambda key, value, error: seen.append((key, value, QThread.currentThread())))
    try:
        task.submit("A", slow)
        assert started.wait(1)
        task.submit("B", lambda: calls.append("B"))
        task.submit("C", lambda: calls.append("C") or "new")
        release.set()
        wait(lambda: bool(seen))
        assert calls == ["A", "C"]
        assert seen == [("C", "new", qapp.thread())]
    finally:
        release.set()
        task.cancel()


def test_failed_read_does_not_block_the_next_independent_read(qapp):
    task = LatestTask()
    out = []
    task.completed.connect(lambda *args: out.append(args))
    task.submit("bad", lambda: 1 / 0)
    wait(lambda: len(out) == 1)
    assert isinstance(out[0][2], ZeroDivisionError)
    task.submit("good", lambda: 42)
    wait(lambda: len(out) == 2)
    assert out[1] == ("good", 42, None)
    task.cancel()


def test_older_rank_tracks_never_replace_voice_candidates_after_a_new_rank_request(setup_view):
    s = setup_view
    a_started, c_started = threading.Event(), threading.Event()
    a_release, c_release = threading.Event(), threading.Event()
    calls = []
    def read(source, list_id, page=1):
        calls.append(list_id)
        started, release = (a_started, a_release) if list_id == "A" else (c_started, c_release)
        started.set()
        assert release.wait(1)
        return {"tracks": [OnlineTrack("source", list_id, list_id)], "is_end": True}
    s.c.get_top_list_detail = read
    s.c.set_voice_rank_tracks = Mock()
    try:
        s.panel._load_rank_detail("source", "A", "旧榜单")
        assert a_started.wait(1)
        s.panel._load_rank_detail("source", "B", "中间榜单")
        s.panel._load_rank_detail("source", "C", "新榜单")
        a_release.set()
        wait(c_started.is_set)
        assert not s.c.set_voice_rank_tracks.called, "旧结果不可替换语音可选曲目，即使只短暂出现"
        c_release.set()
        wait(lambda: s.panel.list_rank_songs.count() == 1)
        assert s.panel._rank_tracks[0].track_id == "C" and calls == ["A", "C"]
    finally:
        a_release.set()
        c_release.set()
        wait(lambda: s.panel._rank_detail_thread is None or not s.panel._rank_detail_thread.isRunning())


def test_normal_search_does_not_reload_discovery_but_plugin_refresh_does(setup_view):
    s = setup_view
    s.panel.discovery.refresh_sources = Mock()
    s.c.search_online = Mock(return_value={})
    s.panel.edit_kw.setText("普通搜索")
    s.panel._do_search()
    wait(lambda: s.panel._thread is None)
    s.panel.discovery.refresh_sources.assert_not_called()
    s.panel.refresh_online_sources()
    s.panel.discovery.refresh_sources.assert_called_once_with()


def test_music_discovery_uses_capabilities_and_routes_real_tracks_without_playing(qapp):
    tracks = [OnlineTrack("source", "version-id", "歌曲现场版", "歌手", "现场专辑")]
    def browse(source, operation, **params):
        assert QThread.currentThread() != qapp.thread()
        if operation == "tags":
            return {"tags": [{"id": "calm", "title": "放松"}]}
        if operation == "sheets":
            return {"data": [{"id": "sheet", "title": "放松歌单"}], "is_end": True}
        return {"tracks": tracks, "is_end": True}
    music = SimpleNamespace(discovery_sources=lambda: [
        {"name": "unsupported", "capabilities": {"sheets": False}},
        {"name": "source", "capabilities": {"sheets": True}}], browse_online=browse)
    widget = MusicDiscovery(music)
    seen = []
    widget.tracks_ready.connect(lambda *args: seen.append(args))
    widget.show()
    wait(lambda: widget.sections["today"].cards.count() == 1)
    assert widget.sources.count() == 1 and widget.tags.currentText() == "放松"
    widget._open_sheet(widget.sections["today"].cards.item(0))
    wait(lambda: bool(seen))
    assert seen == [(tracks, "放松歌单")]
    assert "现场版" in seen[0][0][0].title and widget.back.isVisible()
    widget._task.cancel()
    widget.close()


def test_unsupported_discovery_has_useful_navigation_without_fake_playlists(qapp):
    widget = MusicDiscovery(SimpleNamespace(discovery_sources=lambda: []))
    assert widget.sheets.count() == 0
    assert not widget.refresh_button.isEnabled()
    assert "排行榜" in widget.status.text() and "未提供" in widget.status.text()
    widget.close()


def test_refreshing_plugin_metadata_reloads_visible_discovery_without_restart(qapp):
    names = ["first"]
    def browse(source, operation, **params):
        return {"tags": [{"id": "public", "title": "分类"}]} if operation == "tags" else {
            "data": [{"id": source, "title": source + "歌单"}], "is_end": True}
    music = SimpleNamespace(discovery_sources=lambda: [
        {"name": name, "capabilities": {"sheets": True}} for name in names], browse_online=browse)
    widget = MusicDiscovery(music)
    widget.show()
    wait(lambda: widget.sections["today"].cards.count() == 1)
    names[:] = ["second"]
    widget.refresh_sources()
    wait(lambda: widget.sections["today"].cards.count() == 1 and "second" in widget.sections["today"].cards.item(0).text())
    assert widget.sources.currentData() == "second"
    widget._task.cancel()
    widget.close()


def home(qapp, tmp_path, monkeypatch):
    from app.ui import settings_store
    monkeypatch.setattr(settings_store, "UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    library = MovieLibrary(tmp_path / "library.json", clock=lambda: 1)
    folder = tmp_path / "films"
    folder.mkdir()
    path = folder / "示例电影.2024.mp4"
    path.write_bytes(b"test-only-not-decoded")
    library.scan([str(folder)])
    watch = tmp_path / "watch"
    watch.mkdir()
    key = hashlib.md5(str(path).encode(), usedforsecurity=False).hexdigest().upper()
    (watch / key).write_text("start=65\n", encoding="utf-8")
    movie = SimpleNamespace(mpv=SimpleNamespace(watch_later_dir=watch), get_history=lambda: [
        {"title": "示例电影", "media_type": "local", "replay_value": str(path)}])
    work = {"title": "示例电影", "kind": "movie", "year": 2024,
            "sources": [{"platform": "bilibili", "platform_name": "B站", "rank": 3,
                         "url": "https://www.bilibili.com/bangumi/play/ss1"}]}
    service = SimpleNamespace(read=lambda *a, **kw: {"works": [work], "states": [
        {"platform": "B站", "kind": "movie", "state": "success", "at": 1}]})
    widget = MovieHome(movie, library=library, discovery=service)
    widget.show()
    wait(lambda: widget.local_list.count() and widget.sections["movie"].cards.count())
    return widget, work


def test_movie_home_shows_actual_resume_local_entries_and_platform_provenance(qapp, tmp_path, monkeypatch):
    widget, work = home(qapp, tmp_path, monkeypatch)
    wait(lambda: not widget._local_task._running)
    assert "正在读取" not in widget.local_status.text()
    assert "1:05" in widget.continue_list.item(0).data(DISPLAY_ROLE)["subtitle"]
    assert "2024" in widget.local_list.item(0).data(DISPLAY_ROLE)["subtitle"]
    cards = widget.sections["movie"].cards
    cards.itemClicked.emit(cards.item(0))
    assert "第 3 名" in widget.work_detail.text()
    assert "会员" in widget.work_detail.text()
    assert widget.show_local_matches(work)
    assert "1 个版本" in widget.local_status.text()
    assert "官网入口" in widget.board_status.text()
    assert widget.continue_list.count() == 1 and widget.continue_list.count() <= 3
    widget._local_task.cancel()
    widget._board_task.cancel()
    widget.close()


def test_movie_folder_cancel_does_not_change_user_preferences(qapp, tmp_path, monkeypatch):
    from app.ui import settings_store
    widget, _ = home(qapp, tmp_path, monkeypatch)
    save = Mock()
    monkeypatch.setattr("app.ui.widgets.movie_home.save_ui_prefs", save)
    monkeypatch.setattr("app.ui.widgets.movie_home.QFileDialog.getExistingDirectory", lambda *_: "")
    widget.add_folder()
    save.assert_not_called()
    widget._local_task.cancel()
    widget._board_task.cancel()
    widget.close()


def test_game_workbench_reads_game_clock_reminders_and_cancels_only_selected_id(qapp):
    panel = QWidget()
    panel.lbl_title = QLabel("当前对局 时间 2:01", panel)
    reminders = [{"id": "one", "champion_name": "墨菲特", "skill": "闪现", "remaining": 29,
                  "side": "enemy", "status": "reminded"}]
    cancel = Mock(side_effect=lambda key: reminders.clear() or key == "one")
    manager = SimpleNamespace(get_active_reminders=lambda: list(reminders), cancel=cancel)
    lcu = SimpleNamespace(connected=True, get=Mock(side_effect=AssertionError("must not request LCU")))
    widget = GameWorkbench(panel, manager, lcu)
    widget.show()
    assert "已连接" in widget.connection.text()
    assert "29 秒" in widget.reminders.item(0).text() and "已提醒" in widget.reminders.item(0).text()
    widget.reminders.setCurrentRow(0)
    QTest.mouseClick(widget.cancel_button, Qt.LeftButton)
    cancel.assert_called_once_with("one")
    assert widget.reminders.count() == 0
    lcu.get.assert_not_called()
    widget._timer.stop()
    widget.close()


def test_game_end_and_reconnect_update_view_without_automating_the_game(qapp):
    panel = QWidget()
    panel.lbl_title = QLabel("未在对局中", panel)
    lcu = SimpleNamespace(connected=False)
    widget = GameWorkbench(panel, None, lcu)
    assert "未连接" in widget.connection.text() and not widget.reminders.count()
    lcu.connected = True
    widget.refresh()
    assert "已连接" in widget.connection.text() and "未在对局" in widget.game_status.text()
    widget.close()


@pytest.fixture
def movie_panel(qapp, tmp_path, monkeypatch):
    from app.ui import settings_store
    from app.ui.panels.movie_panel import MoviePanel
    from tests.ui.test_movie_douban_rank import _FakeMovie
    monkeypatch.setattr(settings_store, "UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    movie = _FakeMovie()
    movie.get_history = lambda: []
    movie.embed_enabled = lambda: False
    movie.last_search_error = ""
    panel = MoviePanel(movie=movie)
    panel._poster_mode = False
    panel._timer.stop()
    yield panel, movie
    panel._on_app_quit()
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


def test_movie_search_replaces_waiting_keywords_and_renders_only_latest_on_gui(qapp, movie_panel):
    panel, movie = movie_panel
    started, release = threading.Event(), threading.Event()
    calls, rendered = [], []
    def search(keyword):
        assert QThread.currentThread() != qapp.thread()
        calls.append(keyword)
        if keyword == "A":
            started.set()
            assert release.wait(1)
        return [{"vod_name": keyword}]
    movie.search = search
    original = panel._on_search_done
    def record(result):
        rendered.append((result[0]["vod_name"], QThread.currentThread()))
        original(result)
    panel._on_search_done = record
    try:
        for keyword in ("A", "B", "C"):
            panel.edit_kw.setText(keyword)
            QTest.mouseClick(panel.btn_search, Qt.LeftButton)
            if keyword == "A":
                assert started.wait(1)
        release.set()
        wait(lambda: panel.list_results.count() == 1)
        assert calls == ["A", "C"] and rendered == [("C", qapp.thread())]
        assert panel.list_results.item(0).text() == "C"
    finally:
        release.set()
        wait(lambda: not panel._film_search._running)


@pytest.mark.parametrize("finish", ["home", "clear"])
def test_movie_search_cannot_replace_home_or_cleared_page(movie_panel, finish):
    panel, movie = movie_panel
    started, release = threading.Event(), threading.Event()
    def search(keyword):
        started.set()
        assert release.wait(1)
        return [{"vod_name": keyword}]
    movie.search = search
    panel.edit_kw.setText("old")
    panel._do_search()
    assert started.wait(1)
    try:
        panel._show_movie_home() if finish == "home" else panel._do_clear()
        release.set()
        wait(lambda: not panel._film_search._running)
        assert panel.list_results.count() == 0
        if finish == "home":
            assert panel.stack_results.currentWidget() is panel.movie_home
    finally:
        release.set()


def test_movie_chart_activation_searches_its_title_without_auto_play_or_local_navigation(movie_panel):
    panel, movie = movie_panel
    panel.movie_home.show_local_matches = Mock(return_value=[{"title": "电影"}])
    movie.search = Mock(return_value=[{"vod_name": "电影", "first_url": "https://media.example.invalid/movie"}])
    panel._play_item = Mock()
    panel._find_discovery_work({"title": "电影", "year": 2024})
    wait(lambda: not panel._film_search._running)
    panel.movie_home.show_local_matches.assert_not_called()
    movie.search.assert_called_once_with("电影")
    assert panel.edit_kw.text() == "电影"
    assert panel.stack_results.currentWidget() is panel.list_results
    assert panel.list_results.item(0).data(Qt.UserRole)["vod_name"] == "电影"
    panel._play_item.assert_not_called()


def test_movie_failed_search_has_retry_feedback_instead_of_zero_success(movie_panel):
    panel, movie = movie_panel
    movie.last_search_error = "timeout"
    panel._film_search_finished("movie", [], None)
    assert "搜索未完成" in panel.lbl_status.text() and "重试" in panel.lbl_status.text()


def test_stale_movie_ranks_keep_timestamp_and_error_without_blocking_local_library(qapp, tmp_path, monkeypatch):
    widget, work = home(qapp, tmp_path, monkeypatch)
    widget._boards_loaded_result("all", {"works": [work], "states": [
        {"platform": "B站", "kind": "movie", "state": "stale", "at": 1, "error": "timeout"},
        {"platform": "B站", "kind": "tv", "state": "failed", "error": "network"}]}, None)
    assert "过期缓存" in widget.board_status.text() and "请求超时" in widget.board_status.text()
    assert "连接失败" in widget.board_status.text() and widget.local_list.count() == 1
    assert widget.sections["movie"].cards.count() == 1
    widget._local_task.cancel()
    widget._board_task.cancel()
    widget.close()
