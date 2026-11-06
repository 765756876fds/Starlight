"""Real Qt navigation and bounded catalogue reads with isolated media metadata."""
import threading

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtTest import QTest

from app.music.online.models import CatalogResults
from tests.ui.test_media_homes import music as music, qapp as qapp, wait, works
from tests.ui.test_movie_card_interactions import scene as scene, double_click


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('hidden', [False, True])
def test_source_refresh_during_catalogue_read_runs_latest_request(music, qapp, theme, hidden):
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(qapp).set_theme(theme, persist=False)
    widget, backend = music.widget, music.backend
    started, release = threading.Event(), threading.Event()
    calls = []

    def read():
        calls.append(QThread.currentThread())
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
        result = CatalogResults()
        result['甲'] = [dict(title='榜单', lists=[dict(id='new', title='新目录')])]
        return result

    backend.get_top_lists = read
    widget.show_view('ranks')
    widget._ensure_catalog(force=True)
    wait(started.is_set)
    try:
        if hidden:
            widget.hide()
        for _ in range(3):
            widget.refresh_sources()
        if hidden:
            assert len(calls) == 1  # Wait until the page is visible before starting new work.
            widget.show()
        release.set()
        wait(lambda: not widget._catalog_task._running)
        assert len(calls) == 2, 'refresh must retain exactly one latest catalogue request'
        assert widget.sheets.count() == 1 and widget.sheets.item(0).text() == '新目录'
        assert all(thread != qapp.thread() for thread in calls)
        before = len(calls)
        widget.show_view('new')
        widget.show_view('ranks')
        QTest.qWait(20)
        assert len(calls) == before  # Ordinary navigation keeps the valid catalogue cache.
    finally:
        release.set()
        wait(lambda: not widget._catalog_task._running)


def test_source_refresh_without_inflight_read_does_not_duplicate_request(music):
    widget, backend = music.widget, music.backend
    before = sum(row[0] == 'charts' for row in backend.calls)
    widget.refresh_sources()
    wait(lambda: not widget._catalog_task._running)
    assert sum(row[0] == 'charts' for row in backend.calls) == before + 1


def test_refresh_in_artist_view_defers_catalogue_until_needed(music):
    widget, backend = music.widget, music.backend
    widget.show_view('artists')
    before = sum(row[0] == 'charts' for row in backend.calls)
    widget.hide()
    widget.refresh_sources()
    widget.show()
    QTest.qWait(20)
    assert sum(row[0] == 'charts' for row in backend.calls) == before
    widget.show_view('ranks')
    wait(lambda: not widget._catalog_task._running)
    assert widget.sheets.count() == 12
    assert sum(row[0] == 'charts' for row in backend.calls) == before + 1


def category(scene, kind):
    class Catalog:
        def __init__(self):
            self.calls = []

        def read(self, selected, **kwargs):
            self.calls.append((selected, kwargs))
            return dict(works=works(selected, 60), states=[dict(
                platform='B站', kind=selected, state='success', at=1793853887,
            )])

    scene.home.catalog = catalog = Catalog()
    scene.home.show_category(kind)
    wait(lambda: scene.home.boards.count() == 60)
    scene.home.local_search.setText('作品0')
    scene.home.order.setCurrentIndex(1)
    item = scene.home.boards.item(30)
    scene.home.boards.scrollToItem(item)
    QTest.qWait(30)
    return catalog, item


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('kind', ['movie', 'tv', 'anime', 'variety'])
def test_card_search_returns_to_same_category_filters_and_scroll(scene, qapp, theme, kind):
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(qapp).set_theme(theme, persist=False)
    catalog, item = category(scene, kind)
    old_scroll = scene.home.boards.verticalScrollBar().value()
    title = item.data(Qt.UserRole)['title']
    double_click(scene.home.boards, 30)
    wait(lambda: len(scene.rendered) == 1)
    assert scene.calls == [title]
    assert scene.panel.stack_results.currentWidget() is not scene.home
    before = len(catalog.calls)
    QTest.mouseClick(scene.panel.btn_home, Qt.LeftButton)
    assert scene.home._view == kind
    assert scene.home.local_search.text() == '作品0' and scene.home.order.currentIndex() == 1
    assert scene.home.boards.verticalScrollBar().value() == old_scroll
    assert scene.home.boards.count() == 60 and len(catalog.calls) == before
    assert scene.panel.lbl_status.text() == '已返回刚才的浏览位置'
    assert scene.home._selected_work['title'] == title
    scene.panel._play_item.assert_not_called()
    assert all(thread == qapp.thread() for _, thread in scene.rendered)
    assert scene.panel.btn_home.text() == '电影首页'
    QTest.mouseClick(scene.panel.btn_home, Qt.LeftButton)
    assert scene.home._view == 'all'
    assert scene.panel.lbl_status.text() == '已返回电影首页'


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_manual_search_keeps_explicit_home_behavior(scene, qapp, theme):
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(qapp).set_theme(theme, persist=False)
    _, item = category(scene, 'tv')
    scene.home.boards.itemDoubleClicked.emit(item)
    wait(lambda: len(scene.rendered) == 1)
    scene.panel.edit_kw.setText('普通搜索')
    QTest.mouseClick(scene.panel.btn_search, Qt.LeftButton)
    wait(lambda: len(scene.rendered) == 2)
    QTest.mouseClick(scene.panel.btn_home, Qt.LeftButton)
    assert scene.home._view == 'all'
    scene.panel._play_item.assert_not_called()


def test_return_before_search_finishes_keeps_category_and_ignores_late_result(scene):
    _, item = category(scene, 'anime')
    started, release = threading.Event(), threading.Event()
    scene.releases.append(release)

    def search(keyword):
        started.set()
        assert release.wait(2)
        return [dict(vod_name=keyword, first_url='https://media.invalid/test.m3u8')]

    scene.backend.search = search
    scene.home.boards.itemDoubleClicked.emit(item)
    wait(started.is_set)
    QTest.mouseClick(scene.panel.btn_home, Qt.LeftButton)
    release.set()
    wait(lambda: not scene.panel._film_search._running)
    assert scene.home._view == 'anime'
    assert scene.panel.stack_results.currentWidget() is scene.home
    assert scene.rendered == []
    assert scene.panel.lbl_status.text() == '已返回刚才的浏览位置'
    scene.panel._play_item.assert_not_called()
