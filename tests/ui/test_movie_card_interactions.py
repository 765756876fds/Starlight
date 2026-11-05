"""Real Qt mouse events; isolated chart/search data, no network or playback."""
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from app.movie.library import MovieLibrary
from app.ui.panels.movie_panel import MoviePanel
from app.ui.widgets import movie_home
from tests.ui.test_movie_douban_rank import _FakeMovie


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
    assert predicate(), "Expected Qt state did not arrive within 1.5 seconds"


def works(kind):
    return [dict(title=f"{kind}真实片名{n}", kind=kind, year=2023 + n, platform="bilibili",
                 source_id=f"{kind}-{n}", url=f"https://www.bilibili.com/bangumi/play/ss{n + 1}",
                 sources=[dict(platform="bilibili", platform_name="哔哩哔哩", source_id=f"{kind}-{n}",
                               url=f"https://www.bilibili.com/bangumi/play/ss{n + 1}", rank=n + 1)])
            for n in range(2)]


@pytest.fixture
def scene(qapp, tmp_path, monkeypatch):
    class Catalog:
        def read(self, kind, **kwargs):
            kinds = ("movie", "tv", "anime", "variety") if kind == "all" else (kind,)
            return dict(works=[row for k in kinds for row in works(k)],
                        states=[dict(platform="哔哩哔哩", kind=k, state="success", at=1000) for k in kinds])
    monkeypatch.setattr(movie_home, "MovieDiscovery", lambda _: Catalog())
    class IsolatedLibrary(MovieLibrary):
        def __init__(self, _):
            super().__init__(tmp_path / "library.json")
    monkeypatch.setattr(movie_home, "MovieLibrary", IsolatedLibrary)
    monkeypatch.setattr("app.ui.settings_store.UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    backend = _FakeMovie()
    backend.get_history = lambda: []
    backend.embed_enabled = lambda: False
    backend.last_search_error = ""
    backend.last_search_source = None
    backend.history_item = lambda row: dict(vod_name=row["title"], first_url=row["replay_value"], local_file=True)
    calls, threads, rendered = [], [], []
    def search(keyword):
        calls.append(keyword)
        threads.append(QThread.currentThread())
        return [dict(vod_id=keyword, vod_name=keyword, first_url="https://media.example.invalid/movie.m3u8")]
    backend.search = search
    panel = MoviePanel(movie=backend)
    panel._timer.stop()
    panel._play_item = Mock()
    original = panel._on_search_done
    def render(rows):
        rendered.append((deepcopy(rows), QThread.currentThread()))
        original(rows)
    panel._on_search_done = render
    notices = Mock()
    monkeypatch.setattr("app.ui.panels.movie_panel.notify", notices)
    panel.resize(1080, 820)
    panel.show()
    home = panel.movie_home
    wait(lambda: home._boards_loaded and not home._local_task._running)
    state = SimpleNamespace(panel=panel, home=home, backend=backend, calls=calls, threads=threads,
                            rendered=rendered, notices=notices, releases=[])
    yield state
    for release in state.releases:
        release.set()
    wait(lambda: not panel._film_search._running)
    panel._on_app_quit()
    panel.close()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def cards(scene, kind, preview=False):
    p, home = scene.panel, scene.home
    if p.stack_results.currentWidget() is not home:
        QTest.mouseClick(p.btn_home, Qt.LeftButton)
    home.show_category("all" if preview else kind)
    view = home.sections[kind].cards if preview else home.boards
    wait(lambda: view.count() == 2)
    if preview:
        home.home.ensureWidgetVisible(view)
    QTest.qWait(10)
    return view


def select(view, index=0):
    item = view.item(index)
    view.scrollToItem(item)
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(item).center())
    QTest.qWait(10)
    return item


def double_click(view, index=0):
    item = select(view, index)
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(item).center())


def test_primary_navigation_order_gives_variety_its_own_entry_and_discovery_remains_an_overview(scene):
    assert list(scene.home.nav_buttons) == ["movie", "tv", "anime", "variety", "all", "local"]
    assert [b.text() for b in scene.home.nav_buttons.values()] == ["电影", "电视剧", "动漫", "综艺", "发现", "本地影片"]
    assert "variety" in scene.home.sections
    QTest.mouseClick(scene.home.nav_buttons["variety"], Qt.LeftButton)
    wait(lambda: scene.home._view == "variety" and scene.home.boards.count() == 2)
    assert scene.home.nav_buttons["variety"].isChecked() and not scene.home.nav_buttons["all"].isChecked()
    assert scene.home.category_title.text() == "综艺"
    QTest.mouseClick(scene.home.nav_buttons["all"], Qt.LeftButton)
    assert scene.home.pages.currentWidget() is scene.home.home and scene.home._view == "all"
    assert scene.home.nav_buttons["all"].isChecked()
    assert all(section.cards.count() == 2 for section in scene.home.sections.values())


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_variety_entry_reuses_existing_category_request_and_source_filters(scene, qapp, theme):
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(qapp).set_theme(theme, persist=False)
    original = scene.home.catalog.read
    def read(kind, **kwargs):
        result = original(kind, **kwargs)
        for row, (key, name, url) in zip(result["works"], [
                ("iqiyi", "爱奇艺", "https://www.iqiyi.com/v_fixture.html"),
                ("tencent", "腾讯视频", "https://v.qq.com/x/search/?q=fixture")]):
            row["sources"] = [dict(platform=key, platform_name=name, source_id=row["source_id"], url=url, rank=1)]
        return result
    request = Mock(side_effect=read)
    scene.home.catalog.read = request
    QTest.mouseClick(scene.home.nav_buttons["variety"], Qt.LeftButton)
    wait(lambda: not scene.home._board_task._running and scene.home.boards.count() == 2)
    request.assert_called_once_with("variety", force=False)
    assert scene.home.pages.currentWidget() is scene.home.category_page
    for key, expected in [("iqiyi", "variety-0"), ("tencent", "variety-1")]:
        scene.home.source_filter.setCurrentIndex(scene.home.source_filter.findData(key))
        assert scene.home.boards.count() == 1
        row = scene.home.boards.item(0).data(Qt.UserRole)
        assert row["kind"] == "variety" and row["source_id"] == expected
    assert not scene.calls and not scene.backend.play_calls


@pytest.mark.parametrize("state", ["empty", "failed"])
def test_variety_without_available_data_never_invents_cards_or_reports_success(scene, monkeypatch, state):
    notice = Mock()
    monkeypatch.setattr(movie_home, "notify", notice)
    scene.home.catalog.read = Mock(return_value=dict(works=[], states=[
        dict(platform="爱奇艺", kind="variety", state=state, at=None, error="timeout" if state == "failed" else None)]))
    QTest.mouseClick(scene.home.nav_buttons["variety"], Qt.LeftButton)
    wait(lambda: not scene.home._board_task._running)
    assert scene.home.boards.count() == 0 and scene.home.category_empty.isVisible()
    assert scene.home.nav_buttons["variety"].isChecked()
    assert notice.call_args.kwargs["status"] == ("failed" if state == "failed" else "warning")
    expected = "未完成" if state == "failed" else "暂无榜单内容"
    assert expected in notice.call_args.args[0]
    assert not scene.calls and not scene.backend.play_calls


def test_redundant_find_action_is_removed(scene):
    assert not hasattr(scene.home, "find_button")
    assert all(b.text() != "在星芒找这部" for b in scene.home.detail.findChildren(QPushButton))


@pytest.mark.parametrize("kind", ["movie", "tv", "anime", "variety"])
@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_single_click_selects_but_double_click_searches_exactly_once_on_both_themes(scene, qapp, kind, preview, theme):
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(qapp).set_theme(theme, persist=False)
    view = cards(scene, kind, preview)
    item = select(view, 1)
    row = item.data(Qt.UserRole)
    assert scene.home._selected_work["source_id"] == row["source_id"]
    assert row["title"] in scene.home.work_detail.text() and scene.home.detail.isVisible()
    assert not scene.calls and not scene.backend.play_calls
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(item).center())
    wait(lambda: not scene.panel._film_search._running)
    assert scene.calls == [row["title"]]
    assert scene.panel.edit_kw.text() == row["title"]
    assert scene.panel.stack_results.currentWidget() is scene.panel.list_posters
    assert scene.panel.list_posters.item(0).data(Qt.UserRole)["vod_name"] == row["title"]
    assert len(scene.threads) == 1 and scene.threads[0] != qapp.thread()
    assert len(scene.rendered) == 1 and scene.rendered[0][1] == qapp.thread()
    assert not scene.backend.play_calls
    scene.panel._play_item.assert_not_called()


def test_sorted_duplicate_titles_search_the_selected_stable_payload_not_a_list_position(scene):
    view = cards(scene, "movie")
    rows = works("movie")
    rows[0]["title"] = rows[1]["title"] = "同名不同年份作品"
    scene.home._category_rows = rows
    scene.home.order.setCurrentIndex(2)
    assert view.item(0).data(Qt.UserRole)["source_id"] == "movie-1"
    chosen = []
    scene.home.find_work.connect(chosen.append)
    double_click(view, 0)
    wait(lambda: not scene.panel._film_search._running)
    assert len(chosen) == 1 and chosen[0]["source_id"] == "movie-1" and chosen[0]["year"] == 2024
    assert scene.calls == ["同名不同年份作品"]


def test_slow_previous_film_cannot_replace_latest_double_clicked_film(scene, qapp):
    started, release = threading.Event(), threading.Event()
    scene.releases.append(release)
    def search(keyword):
        scene.calls.append(keyword)
        if keyword == "movie真实片名0":
            started.set()
            assert release.wait(2)
        return [dict(vod_name=keyword, first_url="https://media.example.invalid/video")]
    scene.backend.search = search
    double_click(cards(scene, "movie"), 0)
    assert started.wait(1)
    double_click(cards(scene, "tv"), 0)
    double_click(cards(scene, "anime"), 1)
    release.set()
    wait(lambda: not scene.panel._film_search._running)
    assert scene.calls == ["movie真实片名0", "anime真实片名1"]
    assert [r[0][0]["vod_name"] for r in scene.rendered] == ["anime真实片名1"]
    assert scene.panel.list_posters.item(0).data(Qt.UserRole)["vod_name"] == "anime真实片名1"
    assert scene.rendered[0][1] == qapp.thread()
    scene.panel._play_item.assert_not_called()


@pytest.mark.parametrize("outcome", ["empty", "empty-sources", "no-sources", "exception", "metadata-only"])
def test_search_empty_or_unavailable_state_has_real_feedback_and_never_plays(scene, outcome):
    def search(keyword):
        scene.calls.append(keyword)
        if outcome == "exception":
            raise RuntimeError("controlled source failure")
        if outcome == "no-sources":
            scene.backend.last_search_error = "无可用源"
        if outcome == "empty-sources":
            scene.backend.last_search_error = "甲: 无结果；乙: 无结果"
        return [dict(vod_name=keyword)] if outcome == "metadata-only" else []
    scene.backend.search = search
    double_click(cards(scene, "movie"))
    wait(lambda: not scene.panel._film_search._running)
    assert scene.calls == ["movie真实片名0"]
    text = scene.panel.lbl_status.text()
    assert any(word in text for word in ("重试", "片源", "片名"))
    assert "播放成功" not in text
    if outcome in ("empty", "empty-sources"):
        assert "没有找到匹配影片" in text
    assert scene.notices.call_args.kwargs["status"] in ("warning", "failed")
    assert scene.panel.list_posters.count() == (1 if outcome == "metadata-only" else 0)
    scene.panel._play_item.assert_not_called()


def test_mixed_source_failure_does_not_become_a_normal_empty_search(scene):
    def search(keyword):
        scene.backend.last_search_error = "甲: 无结果；乙: TimeoutError"
        return []
    scene.backend.search = search
    double_click(cards(scene, "movie"))
    wait(lambda: not scene.panel._film_search._running)
    assert "搜索未完成" in scene.panel.lbl_status.text()
    assert scene.notices.call_args.kwargs["status"] == "failed"
    scene.panel._play_item.assert_not_called()


def test_search_result_selection_reuses_existing_playback_action_after_explicit_double_click(scene):
    double_click(cards(scene, "movie"))
    wait(lambda: not scene.panel._film_search._running)
    scene.panel._play_item.assert_not_called()
    result = scene.panel.list_posters.item(0).data(Qt.UserRole)
    double_click(scene.panel.list_posters)
    scene.panel._play_item.assert_called_once_with(result, index=None)


@pytest.mark.parametrize("work", [None, {}, {"title": " "}, {"title": None}, {"title": ["invalid"]}])
def test_invalid_card_payload_never_submits_a_guessed_search(scene, work):
    scene.panel._find_discovery_work(work)
    assert not scene.calls
    scene.panel._play_item.assert_not_called()


def test_single_selection_keeps_the_actual_official_watch_entry(scene, monkeypatch):
    opened = Mock(return_value=True)
    monkeypatch.setattr(movie_home.QDesktopServices, "openUrl", opened)
    view = cards(scene, "movie")
    selected = select(view, 1).data(Qt.UserRole)
    QTest.mouseClick(scene.home.watch_button, Qt.LeftButton)
    assert opened.call_count == 1 and opened.call_args.args[0].toString() == selected["sources"][0]["url"]
    assert scene.home.watch_button.text() == "官方观看"
    assert not scene.calls and not scene.backend.play_calls


@pytest.mark.parametrize("kind", ["local", "continue"])
def test_local_and_continue_double_click_keep_existing_playback_entry_without_search(scene, tmp_path, kind):
    path = tmp_path / "isolated.mp4"
    path.write_bytes(b"only-an-isolated-filename-not-a-real-movie")
    row = dict(title="本地影片", path=str(path), available=True, media_type="local", replay_value=str(path), resume_seconds=42)
    scene.home._entries = [row]
    scene.home._continuing = [row]
    scene.home.show_category(kind)
    select(scene.home.boards)
    assert not scene.calls
    QTest.mouseDClick(scene.home.boards.viewport(), Qt.LeftButton,
                     pos=scene.home.boards.visualItemRect(scene.home.boards.item(0)).center())
    assert not scene.calls
    scene.panel._play_item.assert_called_once()
