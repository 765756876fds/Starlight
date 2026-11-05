"""Real Qt navigation/rendering with isolated metadata; no media playback/network."""
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QThread, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.movie.library import MovieLibrary
from app.music.online.models import CatalogResults, OnlineTrack
from app.ui.widgets.discovery_cards import DISPLAY_ROLE
from app.ui.widgets.movie_home import MovieHome, film_card
from app.ui.widgets.music_discovery import MusicDiscovery
from tests.ui.test_music_queue_view import setup_view


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
    return app


def wait(predicate, timeout=1800):
    elapsed = 0
    while not predicate() and elapsed < timeout:
        QTest.qWait(10)
        elapsed += 10
    assert predicate(), "expected GUI state did not arrive within the bounded wait"


def works(kind, count=100):
    return [dict(title=f"{kind}作品{i:03}", kind=kind, year=2020 + i % 5 if i else None, cover_url="",
                 sources=[dict(platform="bilibili", platform_name="B站", rank=i + 1,
                               url="https://www.bilibili.com/bangumi/play/ss1")]) for i in range(count)]


class MovieCatalog:
    def __init__(self):
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.slow = False

    def read(self, kind, force=False, **params):
        self.calls.append((kind, force, params, QThread.currentThread()))
        if kind == "all" and self.slow:
            self.entered.set()
            assert self.release.wait(2)
        kinds = ("movie", "tv", "anime") if kind == "all" else () if kind == "variety" else (kind,)
        return {"works": [row for k in kinds for row in works(k)],
                "states": [dict(platform="B站", kind=k, state="success", at=1793853887) for k in kinds]}


@pytest.fixture
def movie(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("app.ui.settings_store.UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    library = MovieLibrary(tmp_path / "library.json")
    folder, watch = tmp_path / "films", tmp_path / "watch"
    folder.mkdir(); watch.mkdir()
    history = []
    for i in range(12):
        path = folder / f"本地影片{i:02}.2024.mp4"
        path.write_bytes(b"isolated-name-index-only")
        history.append(dict(title=f"本地影片{i:02}", media_type="local", replay_value=str(path)))
        key = hashlib.md5(str(path).encode(), usedforsecurity=False).hexdigest().upper()
        (watch / key).write_text(f"start={i + 60}\n", encoding="utf8")
    library.scan([str(folder)])
    backend = SimpleNamespace(get_history=lambda: history, mpv=SimpleNamespace(watch_later_dir=watch))
    catalog = MovieCatalog()
    widget = MovieHome(backend, library=library, discovery=catalog)
    widget.resize(860, 660)
    yield SimpleNamespace(widget=widget, catalog=catalog, library=library)
    catalog.release.set()
    widget.shutdown(); widget.close(); widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def show_movie(s):
    s.widget.show()
    wait(lambda: s.widget._boards_loaded and not s.widget._local_task._running)


def test_movie_home_previews_are_bounded_separate_and_never_build_a_299_item_list(movie, qapp):
    show_movie(movie)
    w = movie.widget
    assert w.continue_list.count() == 3 and w.local_list.count() == 6
    assert {k: section.cards.count() for k, section in w.sections.items()} == {"movie": 6, "tv": 6, "anime": 6, "variety": 0}
    assert w.boards.count() == 0 and w.pages.currentWidget() is w.home
    for kind, section in w.sections.items():
        assert all(section.cards.item(i).data(Qt.UserRole)["kind"] == kind for i in range(section.cards.count()))
    assert "未提供" in w.sections["variety"].hint.text()
    assert movie.catalog.calls[0][0:3] == ("all", False, {"limit": 6})
    assert movie.catalog.calls[0][3] != qapp.thread()
    unknown = w.sections["movie"].cards.item(0).data(DISPLAY_ROLE)
    assert "未知" not in unknown["subtitle"] and not unknown["cover_url"]
    assert not w.grab().isNull(), "actual placeholder painting must succeed"


def test_view_all_loads_only_selected_category_and_return_home_keeps_previews(movie):
    show_movie(movie)
    w = movie.widget
    QTest.mouseClick(w.nav_buttons["tv"], Qt.LeftButton)
    wait(lambda: w.boards.count() == 100)
    assert w.pages.currentWidget() is w.category_page and w.category_title.text() == "电视剧"
    assert all(w.boards.item(i).data(Qt.UserRole)["kind"] == "tv" for i in range(100))
    assert movie.catalog.calls[-1][0] == "tv"
    wait(lambda: w.boards.verticalScrollBar().maximum() > 0)
    w.local_search.setText("作品099")
    assert w.boards.count() == 1
    before = len(movie.catalog.calls)
    QTest.mouseClick(w.nav_buttons["all"], Qt.LeftButton)
    assert len(movie.catalog.calls) == before and w.sections["movie"].cards.count() == 6
    QTest.mouseClick(w.sections["movie"].all_button, Qt.LeftButton)
    wait(lambda: w.boards.count() == 100)
    assert movie.catalog.calls[-1][0] == "movie"


def test_old_home_request_cannot_replace_new_category_and_local_remains_available(movie):
    w, catalog = movie.widget, movie.catalog
    catalog.slow = True
    w.show()
    assert catalog.entered.wait(1)
    wait(lambda: w.local_list.count() == 6)
    QTest.mouseClick(w.nav_buttons["movie"], Qt.LeftButton)
    QTest.mouseClick(w.nav_buttons["tv"], Qt.LeftButton)
    catalog.release.set()
    wait(lambda: w.boards.count() == 100)
    assert [c[0] for c in catalog.calls] == ["all", "tv"]
    assert w.category_title.text() == "电视剧" and w.pages.currentWidget() is w.category_page
    assert not w._boards_loaded, "discarded home result must not mark the home as freshly rendered"


def test_platform_filter_and_official_entry_cannot_relabel_another_platforms_ranking(movie, monkeypatch):
    show_movie(movie)
    w = movie.widget
    w.show_category("movie")
    wait(lambda: w.boards.count() == 100)
    row = works("movie", 1)[0]
    row["sources"].append(dict(platform="iqiyi", platform_name="爱奇艺", rank=5, url="https://www.iqiyi.com/v_demo.html"))
    w._boards_loaded_result("movie", {"works": [row], "states": []}, None)
    assert w.source_filter.findData("iqiyi") >= 0 and w.source_filter.findData("youku") == -1
    w.boards.itemClicked.emit(w.boards.item(0))
    assert "B站：第 1 名" in w.work_detail.text() and "爱奇艺：第 5 名" in w.work_detail.text()
    open_url = Mock(return_value=True)
    monkeypatch.setattr("app.ui.widgets.movie_home.QDesktopServices.openUrl", open_url)
    w.open_official("youku")
    assert open_url.call_args.args[0].host() == "www.youku.com"
    assert w.boards.item(0).data(DISPLAY_ROLE)["caption"] == "B站 #1 / 爱奇艺 #5"


def test_empty_local_library_has_no_blank_preview_rail_and_no_online_dependency(movie):
    w = movie.widget
    w._entries, w._continuing = [], []
    w._render_local()
    assert w.continue_list.isHidden() and w.local_list.isHidden()
    assert "添加" in w.local_status.text() and w.recent_section.hint.text()
    w.show_category("local")
    assert not movie.catalog.calls
    assert w.boards.count() == 0


def test_empty_and_partial_rank_failures_are_not_the_same_status(movie):
    show_movie(movie)
    w = movie.widget
    w._boards_loaded_result("all", {"works": works("tv", 2), "states": [
        dict(platform="B站", kind="movie", state="failed", error="timeout"),
        dict(platform="B站", kind="tv", state="success", at=1793853887)]}, None)
    assert w.sections["tv"].cards.count() == 2
    assert "未能读取" in w.sections["movie"].hint.text() and "超时" in w.board_status.text()
    assert w.local_list.count() == 6
    assert film_card(dict(title="电影", kind="movie", year=None))["subtitle"] == "电影"


def test_unconnected_variety_category_never_reports_a_successful_refresh(movie, monkeypatch):
    show_movie(movie)
    post = Mock()
    monkeypatch.setattr("app.ui.widgets.movie_home.notify", post)
    movie.widget.show_category("variety")
    wait(lambda: not movie.widget._board_task._running)
    assert post.call_args.kwargs["status"] == "warning"
    assert "尚未接入" in post.call_args.args[0]


def test_filtering_a_selected_film_clears_the_old_action_target(movie):
    show_movie(movie)
    w = movie.widget
    w.show_category("movie")
    wait(lambda: w.boards.count() == 100)
    w.boards.itemClicked.emit(w.boards.item(0))
    assert w._selected_work is not None
    w.local_search.setText("没有这部影片")
    assert w.boards.count() == 0
    assert w._selected_work is None and w.detail.isHidden()


def test_real_card_painting_handles_cached_poster_and_the_missing_poster_placeholder(qapp):
    from PySide6.QtGui import QPixmap, QColor
    from app.ui.widgets.discovery_cards import CardList
    from app.ui.widgets.music_card_delegate import _get_cover_loader
    loader = _get_cover_loader()
    image = QPixmap(90, 140)
    image.fill(QColor("#52728a"))  # Test image only, never a product theme color.
    url = "https://example.invalid/isolated-poster.jpg"
    loader._covers["net:" + url] = image
    view = CardList(portrait=True)
    view.resize(370, 260)
    view.set_rows([dict(title="有海报", kind="movie", cover_url=url), dict(title="无海报", kind="tv")], film_card)
    view.show()
    QTest.qWait(30)
    assert not view.grab().isNull()
    assert view.count() == 2
    assert view.visualItemRect(view.item(0)).width() == view.gridSize().width()
    view.close(); view.deleteLater()


class MusicBackend:
    def __init__(self):
        self.calls = []
        self.names = ["甲", "乙"]
    def discovery_sources(self):
        return [dict(name=n, capabilities=dict(charts=True, sheets=True, artist=True, album=True)) for n in self.names]
    def get_top_lists(self):
        self.calls.append(("charts", QThread.currentThread()))
        result = CatalogResults()
        for name in self.names:
            result[name] = [dict(title="榜单", lists=[dict(id=str(i), title=("新歌" if i < 6 else "热歌") + str(i)) for i in range(12)])]
        return result
    def browse_online(self, source, operation, **params):
        self.calls.append((source, operation, dict(params), QThread.currentThread()))
        if operation == "tags": return {"tags": [dict(id="public", title="放松")]}
        if operation in ("sheets", "artists"):
            return dict(data=[dict(id=str(i), title=f"{source}{operation}{i}") for i in range(20)], is_end=params.get("page", 1) > 1)
        return dict(tracks=[OnlineTrack(source, str(params.get("page", 1)), "现场版", album="Live")], is_end=params.get("page", 1) > 1)
    def get_top_list_detail(self, source, list_id, page=1):
        self.calls.append(("chart_tracks", source, list_id, page))
        return {"tracks": [OnlineTrack(source, list_id, "曲目")], "is_end": True}
    def get_history(self, limit=200):
        self.calls.append(("history_limit", limit))
        return [dict(title=f"听过{i}", source_type="local") for i in range(min(limit, 20))]
    def get_playlists(self):
        return [dict(id=i, name=f"我的歌单{i}") for i in range(10)]


@pytest.fixture
def music(qapp):
    backend = MusicBackend()
    widget = MusicDiscovery(backend)
    widget.resize(850, 650)
    widget.show()
    wait(lambda: widget.sections["today"].cards.count() == 6 and widget.sections["ranks"].cards.count() == 6
         and widget.sections["recent"].cards.count() == 6)
    yield SimpleNamespace(widget=widget, backend=backend)
    widget.shutdown(); widget.close(); widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_music_home_has_six_bounded_previews_without_loading_all_chart_tracks(music, qapp):
    w, backend = music.widget, music.backend
    assert len(w.nav_buttons) == 6
    assert all(section.cards.count() == 6 for section in w.sections.values())
    today = [w.sections["today"].cards.item(i).data(Qt.UserRole)["id"] for i in range(6)]
    featured = [w.sections["featured"].cards.item(i).data(Qt.UserRole)["id"] for i in range(6)]
    assert set(today).isdisjoint(featured)
    assert ("history_limit", 8) in backend.calls
    assert not any(c[0] == "chart_tracks" for c in backend.calls)
    assert all(c[-1] != qapp.thread() for c in backend.calls if c[0] == "甲")
    before = len(backend.calls)
    QTest.mouseClick(w.nav_buttons["ranks"], Qt.LeftButton)
    assert w.sheets.count() == 12 and len(backend.calls) == before
    QTest.mouseClick(w.nav_buttons["new"], Qt.LeftButton)
    assert w.sheets.count() == 6
    assert all("新歌" in w.sheets.item(i).text() for i in range(6))


def test_music_artist_album_and_sheet_details_are_loaded_only_on_request(music):
    w, backend = music.widget, music.backend
    QTest.mouseClick(w.nav_buttons["artists"], Qt.LeftButton)
    assert not any(len(c) > 1 and c[1] == "artists" for c in backend.calls)
    w.artist_query.setText("名称")
    QTest.mouseClick(w.artist_search, Qt.LeftButton)
    wait(lambda: w.sheets.count() == 20)
    assert backend.calls[-1][2]["kind"] == "artist"
    out = []
    w.tracks_ready.connect(lambda tracks, title: out.append(tracks))
    w._open_category_item(w.sheets.item(0))
    wait(lambda: len(out) == 1)
    assert out[0][0].source_name == "甲" and out[0][0].album == "Live"
    w._more()
    wait(lambda: len(out) == 2)
    assert [t.track_id for t in out[-1]] == ["1", "2"]
    w.artist_kind.setCurrentIndex(1)
    QTest.mouseClick(w.artist_search, Qt.LeftButton)
    wait(lambda: w.sheets.count() == 20)
    assert backend.calls[-1][2]["kind"] == "album"


def test_music_source_switch_and_history_playlist_shortcuts_preserve_provenance(music):
    w = music.widget
    w.sources.setCurrentIndex(1)
    wait(lambda: w.sections["today"].cards.count() and w.sections["today"].cards.item(0).data(Qt.UserRole)["source_name"] == "乙")
    assert all(w.sections["ranks"].cards.item(i).data(Qt.UserRole)["source_name"] == "乙" for i in range(6))
    visited, history, playlists = [], [], []
    w.navigate.connect(visited.append)
    w.history_chosen.connect(history.append)
    w.playlist_chosen.connect(playlists.append)
    w.sections["recent"].cards.itemDoubleClicked.emit(w.sections["recent"].cards.item(0))
    w.sections["mine"].cards.itemDoubleClicked.emit(w.sections["mine"].cards.item(0))
    QTest.mouseClick(w.nav_buttons["history"], Qt.LeftButton)
    assert history[0]["title"] == "听过0" and playlists[0]["id"] == 0 and visited == [2]


def test_music_return_home_reuses_browse_cache_but_explicit_refresh_reaches_plugin(music):
    w, backend = music.widget, music.backend
    before = sum(len(c) > 1 and c[1] == "sheets" for c in backend.calls)
    w.show_view("sheets")
    wait(lambda: w.sheets.count() == 20)
    w.show_view("home")
    wait(lambda: not w._task._running)
    assert sum(len(c) > 1 and c[1] == "sheets" for c in backend.calls) == before
    QTest.mouseClick(w.refresh_button, Qt.LeftButton)
    wait(lambda: sum(len(c) > 1 and c[1] == "sheets" for c in backend.calls) == before + 1)


def test_late_artist_result_cannot_replace_the_new_rank_category(music):
    w, backend = music.widget, music.backend
    started, release = threading.Event(), threading.Event()
    original = backend.browse_online
    def read(source, operation, **params):
        if operation == "artists":
            started.set()
            assert release.wait(2)
            return {"data": [{"id": "old", "title": "旧歌手结果"}], "is_end": True}
        return original(source, operation, **params)
    backend.browse_online = read
    try:
        w.show_view("artists")
        w.artist_query.setText("名称")
        QTest.mouseClick(w.artist_search, Qt.LeftButton)
        assert started.wait(1)
        w.show_view("ranks")
        release.set()
        wait(lambda: not w._task._running)
        assert w._view == "ranks" and w.sheets.count() == 12
        assert all("旧歌手" not in w.sheets.item(i).text() for i in range(12))
    finally:
        release.set()


def test_failed_sheet_read_can_retry_without_being_cached_as_empty(music):
    from app.music.online.source import SourceSearchError
    w, backend = music.widget, music.backend
    original = backend.browse_online
    calls = []
    def read(source, operation, **params):
        if operation == "sheets":
            calls.append(params["page"])
            if len(calls) == 1:
                raise SourceSearchError("timeout")
        return original(source, operation, **params)
    backend.browse_online = read
    w._browse_cache.clear()
    w.show_view("sheets")
    wait(lambda: not w._task._running)
    assert "超时" in w.status.text() and "重试" in w.empty.text()
    QTest.mouseClick(w.refresh_button, Qt.LeftButton)
    wait(lambda: w.sheets.count() == 20)
    assert calls == [1, 1]


def test_unsupported_music_capabilities_have_empty_states_and_no_fabricated_requests(qapp):
    backend = SimpleNamespace(discovery_sources=lambda: [], browse_online=Mock())
    w = MusicDiscovery(backend)
    w.show()
    assert all(section.cards.count() == 0 for section in w.sections.values())
    QTest.mouseClick(w.nav_buttons["artists"], Qt.LeftButton)
    assert not w.artist_search.isEnabled() and "不支持" in w.empty.text()
    backend.browse_online.assert_not_called()
    w.shutdown(); w.close(); w.deleteLater()


def test_search_results_are_separate_from_home_and_late_results_do_not_steal_navigation(setup_view):
    s = setup_view
    panel = s.panel
    snapshot = s.c.get_queue_snapshot()
    assert panel.online_stack.currentWidget() is panel.discovery
    panel.edit_kw.setText("song")
    s.c.search_online = Mock(return_value={"test": [OnlineTrack("test", "1", "song")]})
    panel._do_search()
    panel._show_discovery_home()
    wait(lambda: panel._thread is None)
    assert panel.online_stack.currentWidget() is panel.discovery
    assert panel.list_results.count() == 1
    assert s.c.get_queue_snapshot() == snapshot and not s.mpv.mock_calls


def test_music_version_filter_preserves_distinct_sources_and_row_mapping(setup_view):
    p = setup_view.panel
    tracks = [OnlineTrack("甲", "same-id", "同名"), OnlineTrack("乙", "same-id", "同名 Live"),
              OnlineTrack("丙", "cover", "同名 翻唱")]
    p._results_requested = True
    p._on_search_done({"test": tracks})
    assert p._tracks_by_row == tracks
    p.cb_search_filter.setCurrentIndex(1)
    assert p._tracks_by_row == [tracks[1]]
    p.cb_search_filter.setCurrentIndex(2)
    assert p._tracks_by_row == [tracks[2]]
    p.cb_search_filter.setCurrentIndex(0)
    assert p._tracks_by_row == tracks and p.list_results.count() == 3


def test_recent_and_own_playlist_cards_reuse_existing_panel_handlers_once(setup_view):
    s = setup_view
    snapshot = s.c.get_queue_snapshot()
    s.panel._play_async = Mock()
    row = {"title": "最近听过", "source_type": "local", "music_id": 7}
    s.panel.discovery.history_chosen.emit(row)
    s.panel._play_async.assert_called_once()
    assert s.panel._play_async.call_args.args == (s.c.play_history_item, row)
    s.c.get_playlists = Mock(return_value=[{"id": 1, "name": "第一份"}, {"id": 2, "name": "第二份"}])
    s.c.get_playlist_songs = Mock(return_value=[])
    s.panel.discovery.playlist_chosen.emit({"id": 2, "name": "第二份"})
    assert s.panel.tabs.currentIndex() == 3
    assert s.panel.list_playlists.currentItem().data(Qt.UserRole)["id"] == 2
    assert s.c.get_queue_snapshot() == snapshot and not s.mpv.mock_calls
