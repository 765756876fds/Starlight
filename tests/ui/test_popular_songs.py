import threading
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from app.music.online.models import OnlineTrack
from app.ui.widgets.popular_songs import PopularSongs
from tests.ui.test_music_queue_view import setup_view, qapp, wait_for


def song(source, title="海边", artist="甲", **extra):
    return OnlineTrack(source, source + title, title, artist, "专辑", 181, extra=extra)


def test_default_panel_uses_selected_original_track_and_partial_success(setup_view):
    s = setup_view
    panel = s.panel
    hot = panel.popular_songs
    assert panel.tabs.currentIndex() == 4 and not panel.rank_mode.isChecked()
    a, b = song("甲源", _raw={"id":"a"}), song("乙源", _raw={"id":"b"})
    entered, release = threading.Event(), threading.Event()
    requests = []
    s.c.discovery_sources = lambda: [dict(name=n, capabilities={"charts":True}) for n in ("甲源","乙源","丙源","丁源")]
    def catalog(source):
        requests.append(source)
        if source == "乙源":
            entered.set()
            assert release.wait(3)
        if source == "丙源":
            raise TimeoutError()
        return {source:[{"lists":[{"id":"hot", "title":"热歌榜"}]}]}
    s.c.get_top_lists = catalog
    s.c.get_top_list_detail = lambda source, key, *, limit: {"tracks":[a if source == "甲源" else b]}
    hot.load(force=True)
    try:
        wait_for(lambda: entered.is_set() and hot.cards.count() == 1)
        hot.cards.setCurrentRow(0)
    finally:
        release.set()
    wait_for(lambda: not hot.pending)
    assert set(requests) == {"甲源","乙源","丙源"} and hot.cards.count() == 1
    assert "部分榜单" in hot.status.text() and hot.versions.count() == 2
    hot.versions.setCurrentIndex(1)
    called = []
    panel._play_async = lambda target, tracks, index, **kw: called.append((target,tracks[index]))
    QTest.mouseClick(hot.play, Qt.LeftButton)
    assert called[0][0] == s.c.play_online_list and called[0][1] is b
    assert b.extra == {"_raw":{"id":"b"}}
    assert s.c.queue.size() == 3


def test_hidden_page_discards_late_detail_and_cached_read_reuses_rows(qapp):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def detail(source, key, *, limit):
        assert limit == 12
        calls.append(source)
        entered.set()
        assert release.wait(3)
        return {"tracks":[song(source)]}
    backend = SimpleNamespace(discovery_sources=lambda:[{"name":"A","capabilities":{"charts":True}}],
                              get_top_lists=lambda s:{s:[{"lists":[{"id":"h","title":"热歌榜"}]}]},
                              get_top_list_detail=detail)
    page = PopularSongs(backend)
    page.show()
    try:
        wait_for(entered.is_set)
        page.hide()
        release.set()
        wait_for(lambda: not any(t._running for t in page.tasks))
        assert page.cards.count() == 0
        page.show()
        wait_for(lambda: page.cards.count() == 1)
        page.hide()
        page.show()
        assert calls == ["A","A"]
    finally:
        release.set()
        page.shutdown()
        page.close()



def test_shutdown_while_directory_is_read_does_not_start_chart_detail(qapp):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def catalog(source):
        entered.set()
        assert release.wait(3)
        return {source: [{"lists": [{"id": "h", "title": "热歌榜"}]}]}

    page = PopularSongs(SimpleNamespace(
        discovery_sources=lambda: [{"name": "A", "capabilities": {"charts": True}}],
        get_top_lists=catalog,
        get_top_list_detail=lambda *args, **kwargs: calls.append(args)))
    page.show()
    try:
        wait_for(entered.is_set)
        page.shutdown()
        page.close()
    finally:
        release.set()
    wait_for(lambda: not any(task._running for task in page.tasks))
    assert not calls and page.cards.count() == 0
