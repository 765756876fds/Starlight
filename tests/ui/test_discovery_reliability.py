from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.music.online.models import OnlineTrack
from app.ui.widgets.music_discovery import MusicDiscovery
from app.ui.widgets.movie_home import MovieHome
from app.movie.discovery import MovieDiscovery
from app.movie.library import MovieLibrary
from app.movie.rank_sources import TencentRank
from tests.unit.test_public_rank_sources import tencent_payload


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
    return app


def wait(predicate):
    for _ in range(150):
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


@pytest.mark.parametrize("page", [1, 2])
def test_refreshing_a_chart_page_replaces_that_page_without_duplicating_tracks(qapp, page):
    calls, seen = [], []

    def read(source, list_id, page=1):
        calls.append(page)
        # Preserve versions and duplicate IDs across different actual pages.
        return {"tracks": [OnlineTrack(source, "same-id", f"page-{page}")], "is_end": page == 2}

    music = SimpleNamespace(
        discovery_sources=lambda: [{"name": "demo", "capabilities": {"charts": True}}],
        get_top_lists=lambda: {"demo": [{"title": "榜单", "lists": [{"id": "1", "title": "新歌榜"}]}]},
        get_top_list_detail=read)
    widget = MusicDiscovery(music)
    widget.tracks_ready.connect(lambda tracks, title: seen.append(list(tracks)))
    try:
        widget.show()
        widget.show_view("ranks")
        wait(lambda: widget.sheets.count() == 1)
        widget._open_chart(widget.sheets.item(0))
        wait(lambda: len(seen) == 1)
        if page == 2:
            widget._more()
            wait(lambda: len(seen) == 2)
        QTest.mouseClick(widget.refresh_button, Qt.LeftButton)
        wait(lambda: len(seen) == page + 1)
        assert calls == ([1, 1] if page == 1 else [1, 2, 2])
        assert [track.title for track in seen[-1]] == [f"page-{n}" for n in range(1, page + 1)]
    finally:
        widget.shutdown()
        widget.close()


def test_tencent_cards_and_action_are_honestly_labelled_and_network_runs_off_gui(qapp, tmp_path, monkeypatch):
    from app.ui.widgets import movie_home
    monkeypatch.setattr(movie_home, "load_ui_prefs", lambda: {})
    threads = []
    def fetch(url):
        threads.append(QThread.currentThread())
        return tencent_payload()
    catalog = MovieDiscovery(tmp_path / "cache.json", [TencentRank(fetch)])
    widget = MovieHome(SimpleNamespace(get_history=lambda: []), library=MovieLibrary(tmp_path / "library.json"), discovery=catalog)
    try:
        widget.resize(1000, 700)
        widget.show()
        widget.show_category("movie")
        wait(lambda: widget.boards.count() == 1)
        assert threads and all(thread != qapp.thread() for thread in threads)
        QTest.mouseClick(widget.boards.viewport(), Qt.LeftButton, pos=widget.boards.visualItemRect(widget.boards.item(0)).center())
        assert widget.watch_button.text() == "官方搜索"
        assert "腾讯视频 热搜榜" in widget.work_detail.text()
        assert "不代表可直接播放" in widget.category_status.text()
        widget.show_category("all")
        wait(lambda: widget._preview_result is not None)
        assert "优酷、芒果TV仅提供官网入口" in widget.board_status.text()
        assert "腾讯视频仅提供官网入口" not in widget.board_status.text()
    finally:
        widget.shutdown()
        widget.close()
