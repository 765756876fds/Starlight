"""Real Qt catalogue completion/refresh paths; isolated sources, no network."""
import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.music.online.models import CatalogResults
from app.music.online.source import SourceSearchError
from app.ui.widgets.music_discovery import MusicDiscovery


def wait(predicate):
    for _ in range(150):
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "catalogue did not complete within the bounded Qt wait"


def catalogue(*, title=None, source="甲", errors=None):
    result = CatalogResults()
    if title is not None:
        result[source] = [{"title": "榜单", "lists": [{"id": "new", "title": title}]}]
    result.errors.update(errors or {})
    return result


@pytest.fixture
def scene(monkeypatch):
    app = QApplication.instance() or QApplication([])
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
    outcomes, calls, releases, rendered = deque(), [], [], []
    sources = [{"name": n, "capabilities": {"charts": True}} for n in ("甲", "乙")]

    def read():
        calls.append(QThread.currentThread())
        value = outcomes.popleft()
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value

    backend = SimpleNamespace(discovery_sources=lambda: sources, get_top_lists=read)
    notices = Mock()
    monkeypatch.setattr("app.ui.widgets.music_discovery.notify", notices)
    widget = MusicDiscovery(backend)
    widget.resize(840, 620)
    original = widget._render_charts

    def render():
        rendered.append(QThread.currentThread())
        original()

    widget._render_charts = render
    state = SimpleNamespace(widget=widget, outcomes=outcomes, calls=calls, releases=releases,
                            sources=sources, notices=notices, rendered=rendered, app=app)
    yield state
    for release in releases:
        release.set()
    widget.shutdown()
    wait(lambda: not widget._catalog_task._running)
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def open_new(s, outcome):
    s.outcomes.append(outcome)
    s.widget.show_view("new")
    s.widget.show()
    wait(lambda: not s.widget._catalog_task._running)


@pytest.mark.parametrize("category,word", [("timeout", "超时"), ("plugin", "插件"),
                                         ("network", "网络"), ("unknown", "未完成")])
@pytest.mark.parametrize("owner", ["甲", "js_bridge"])
def test_failed_new_song_directory_is_not_reported_as_unsupported(scene, category, word, owner):
    open_new(scene, catalogue(errors={owner: category}))
    w = scene.widget
    for text in (w.empty.text(), w.sections["new"].hint.text(), w.status.text()):
        assert word in text
        assert "不支持" not in text and "未提供新歌榜" not in text
        assert "刷新" in text or "重试" in text
    assert w.sheets.count() == 0
    assert len(scene.calls) == 1 and scene.calls[0] != scene.app.thread()
    assert all(thread == scene.app.thread() for thread in scene.rendered)


@pytest.mark.parametrize("failure,word", [(SourceSearchError("timeout"), "超时"),
                                         (RuntimeError("private diagnostic"), "未完成"),
                                         (None, "未完成")])
def test_getter_failure_or_unknown_response_does_not_claim_lack_of_support(scene, failure, word):
    open_new(scene, failure)
    assert word in scene.widget.empty.text()
    assert "未提供新歌榜" not in scene.widget.empty.text()
    assert "private diagnostic" not in scene.widget.status.text()


def test_explicitly_unsupported_charts_are_distinct_from_an_empty_response(scene):
    scene.sources[0]["capabilities"] = {"charts": False, "sheets": True}
    # Refresh metadata while hidden, without starting a request.
    scene.widget.refresh_sources()
    open_new(scene, catalogue())
    assert "不支持" in scene.widget.empty.text()
    assert "换" in scene.widget.empty.text()


def test_missing_chart_capability_is_unknown_not_explicitly_unsupported(scene):
    scene.sources[0]["capabilities"] = {"sheets": True}
    scene.widget.refresh_sources()
    open_new(scene, catalogue())
    assert "暂无榜单" in scene.widget.empty.text()
    assert "不支持" not in scene.widget.empty.text()


def test_another_named_plugin_failure_does_not_turn_a_successful_empty_source_into_a_timeout(scene):
    open_new(scene, catalogue(errors={"乙": "timeout"}))
    assert "暂无榜单" in scene.widget.empty.text() and "超时" not in scene.widget.empty.text()
    scene.widget.sources.setCurrentIndex(1)
    assert "超时" in scene.widget.empty.text()
    scene.widget.sources.setCurrentIndex(0)
    assert "暂无榜单" in scene.widget.empty.text()


@pytest.mark.parametrize("result,word", [(catalogue(), "暂无榜单"),
                                       (catalogue(title="热歌榜"), "暂无可识别的新歌榜")])
def test_successfully_read_empty_or_non_new_catalogue_is_not_unsupported(scene, result, word):
    open_new(scene, result)
    assert word in scene.widget.empty.text()
    assert "不支持" not in scene.widget.empty.text()
    assert "失败" not in scene.widget.empty.text() and "超时" not in scene.widget.empty.text()
    assert not scene.notices.called


@pytest.mark.parametrize("owner", ["甲", "乙"])
def test_partial_results_keep_new_songs_and_explain_incompleteness(scene, owner):
    open_new(scene, catalogue(title="新歌榜", errors={owner: "timeout"}))
    assert scene.widget.sheets.count() == 1
    assert scene.widget.sections["new"].cards.count() == 1
    assert "部分" in scene.widget.status.text()
    assert "超时" in scene.widget.status.text()
    assert scene.notices.call_args.kwargs["status"] == "warning"


def test_new_song_retry_reaches_plugin_once_and_success_clears_failure(scene):
    open_new(scene, catalogue(errors={"甲": "timeout"}))
    scene.outcomes.append(catalogue(title="新歌榜"))
    QTest.mouseClick(scene.widget.refresh_button, Qt.LeftButton)
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.calls) == 2
    assert scene.widget.sheets.count() == 1 and scene.widget.empty.text() == ""
    assert "超时" not in scene.widget.status.text()
    assert scene.notices.call_count == 1, "only the original failure should notify"


def test_successful_empty_catalogue_is_cached_until_explicit_retry(scene):
    open_new(scene, catalogue())
    scene.widget.show_view("ranks")
    scene.widget.show_view("new")
    assert len(scene.calls) == 1
    scene.outcomes.append(catalogue(title="新歌榜"))
    QTest.mouseClick(scene.widget.refresh_button, Qt.LeftButton)
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.calls) == 2 and scene.widget.sheets.count() == 1


def test_failed_refresh_keeps_previous_charts_but_marks_them_as_previous_content(scene):
    open_new(scene, catalogue(title="新歌榜"))
    scene.outcomes.append(SourceSearchError("timeout"))
    QTest.mouseClick(scene.widget.refresh_button, Qt.LeftButton)
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.calls) == 2 and scene.widget.sheets.count() == 1
    assert "上次内容" in scene.widget.status.text() and "超时" in scene.widget.status.text()
    assert scene.notices.call_args.kwargs["status"] == "failed"


def test_retry_after_getter_exception_submits_only_one_request(scene):
    open_new(scene, SourceSearchError("timeout"))
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)

    def retry():
        entered.set()
        assert release.wait(2)
        return catalogue(title="新歌榜")

    scene.outcomes.append(retry)
    QTest.mouseClick(scene.widget.refresh_button, Qt.LeftButton)
    assert entered.wait(1)
    release.set()
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.calls) == 2 and scene.widget.sheets.count() == 1


def test_retry_shows_loading_instead_of_the_previous_timeout_while_in_flight(scene):
    open_new(scene, catalogue(errors={"甲": "timeout"}))
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)

    def slow():
        entered.set()
        assert release.wait(2)
        return catalogue(title="新歌榜")

    scene.outcomes.append(slow)
    QTest.mouseClick(scene.widget.refresh_button, Qt.LeftButton)
    assert entered.wait(1)
    assert "正在" in scene.widget.status.text() and "超时" not in scene.widget.status.text()
    assert "正在" in scene.widget.empty.text()
    release.set()
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.calls) == 2 and scene.widget.sheets.count() == 1


def test_catalogue_completion_does_not_replace_a_new_artist_view(scene):
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)

    def slow():
        entered.set()
        assert release.wait(2)
        return catalogue(errors={"甲": "timeout"})

    scene.outcomes.append(slow)
    scene.widget.show_view("new")
    scene.widget.show()
    assert entered.wait(1)
    scene.widget.show_view("artists")
    before = scene.widget.status.text(), scene.widget.empty.text()
    release.set()
    wait(lambda: not scene.widget._catalog_task._running)
    assert scene.widget._view == "artists" and scene.widget.sheets.count() == 0
    assert (scene.widget.status.text(), scene.widget.empty.text()) == before


def test_shutdown_discards_a_late_directory_result(scene):
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)

    def slow():
        entered.set()
        assert release.wait(2)
        return catalogue(title="新歌榜")

    scene.outcomes.append(slow)
    scene.widget.show_view("new")
    scene.widget.show()
    assert entered.wait(1)
    before = len(scene.rendered)
    scene.widget.shutdown()
    release.set()
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.rendered) == before and scene.widget.sheets.count() == 0
    assert not scene.notices.called


def test_loading_is_not_unsupported_and_switching_source_uses_its_own_result(scene):
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)

    def slow():
        entered.set()
        assert release.wait(2)
        return catalogue(title="新歌榜", source="乙", errors={"甲": "timeout"})

    scene.outcomes.append(slow)
    scene.widget.show_view("new")
    scene.widget.show()
    assert entered.wait(1)
    assert "正在" in scene.widget.empty.text() and "未提供" not in scene.widget.empty.text()
    scene.widget.sources.setCurrentIndex(1)
    release.set()
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.calls) == 1
    assert scene.widget.sheets.count() == 1
    assert scene.widget.sheets.item(0).data(Qt.UserRole)["source_name"] == "乙"
    scene.widget.sources.setCurrentIndex(0)
    assert scene.widget.sheets.count() == 0 and "超时" in scene.widget.empty.text()


def test_slow_old_failure_cannot_overwrite_newer_refresh_or_another_view(scene):
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)

    def slow():
        entered.set()
        assert release.wait(2)
        return catalogue(errors={"甲": "timeout"})

    scene.outcomes.extend([slow, catalogue(title="新歌榜")])
    scene.widget.show_view("new")
    scene.widget.show()
    assert entered.wait(1)
    QTest.mouseClick(scene.widget.refresh_button, Qt.LeftButton)
    release.set()
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.calls) == 2 and scene.widget.sheets.count() == 1
    assert "超时" not in scene.widget.status.text() and not scene.notices.called
    assert all(thread == scene.app.thread() for thread in scene.rendered)


def test_refresh_while_first_request_runs_keeps_only_one_new_request(scene):
    entered, release = threading.Event(), threading.Event()
    scene.releases.append(release)

    def slow():
        entered.set()
        assert release.wait(2)
        return catalogue(title="旧新歌榜")

    scene.outcomes.extend([slow, catalogue(title="新歌榜")])
    scene.widget.show_view("new")
    scene.widget.show()
    assert entered.wait(1)
    QTest.mouseClick(scene.widget.refresh_button, Qt.LeftButton)
    QTest.mouseClick(scene.widget.refresh_button, Qt.LeftButton)
    release.set()
    wait(lambda: not scene.widget._catalog_task._running)
    assert len(scene.calls) == 2
    assert scene.widget.sheets.item(0).data(Qt.UserRole)["title"] == "新歌榜"
