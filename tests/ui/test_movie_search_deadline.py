"""Bounded movie search feedback with a real Qt event loop, no external sources."""
import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QThread, QTimer
from PySide6.QtTest import QTest

from app.ui.widgets.latest_task import LatestTask
from tests.ui.test_product_discovery import movie_panel as movie_panel, qapp as qapp, wait
from tests.ui.test_movie_voice_window import scene as scene

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("rows,expected", [
    ([{"vod_name": "公开测试片名", "first_url": "https://synthetic.invalid/movie"}], "找到"),
    ([], "没有找到"),
])
def test_search_finishes_for_result_or_empty(movie_panel, rows, expected):
    panel, movie = movie_panel
    movie.search = Mock(return_value=rows)
    panel.edit_kw.setText("公开测试片名")
    panel.btn_search.click()
    wait(lambda: not panel._film_search._running)
    assert expected in panel.lbl_status.text()
    assert "正在搜索" not in panel.lbl_status.text()
    assert panel.list_results.count() == len(rows)


def test_watchdog_finishes_latest_ui_without_creating_replacement_workers(movie_panel, qapp):
    panel, movie = movie_panel
    assert panel._film_search._timeout_ms == 35000
    panel._film_search._timeout_ms = 150
    started, release = threading.Event(), threading.Event()
    calls, ticks = [], []
    completed = []
    panel._film_search.completed.connect(lambda key, value, error: completed.append(key))
    def search(keyword):
        assert QThread.currentThread() != qapp.thread()
        calls.append(keyword)
        if keyword == "A":
            started.set()
            assert release.wait(2)
        return [{"vod_name": keyword}]
    movie.search = search
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    try:
        panel.edit_kw.setText("A")
        panel._do_search()
        assert started.wait(1)
        for keyword in ("B", "C"):
            panel.edit_kw.setText(keyword)
            panel._do_search()
        wait(lambda: "超时" in panel.lbl_status.text())
        assert panel._film_search._running and panel._film_search._pending is None
        assert calls == ["A"]  # Pending B/C expire, no unbounded parallel reads.
        panel.edit_kw.setText("D")
        panel._do_search()
        assert "超时" in panel.lbl_status.text() and calls == ["A"]
        release.set()
        wait(lambda: not panel._film_search._running)
        assert panel.list_results.count() == 0 and "超时" in panel.lbl_status.text()
        panel.edit_kw.setText("E")
        panel._do_search()
        wait(lambda: panel.list_results.count() == 1)
        assert panel.list_results.item(0).text() == "E"
        assert calls == ["A", "E"]
        assert completed == ["C", "D", "E"]
        assert len(ticks) >= 3
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < .5
    finally:
        timer.stop()
        release.set()
        wait(lambda: not panel._film_search._running)


def test_search_exception_then_new_search_can_succeed(movie_panel):
    panel, movie = movie_panel
    movie.search = Mock(side_effect=[RuntimeError("synthetic failure"), [{"vod_name": "retry"}]])
    for expected in ("搜索未完成", "找到"):
        panel.edit_kw.setText("公开测试片名")
        panel._do_search()
        wait(lambda: not panel._film_search._running)
        assert expected in panel.lbl_status.text()
    assert movie.search.call_count == 2


def test_cancellation_guard_invalidates_running_read_and_does_not_revive_closed_ui(qapp):
    task = LatestTask(timeout_ms=150)
    started, release = threading.Event(), threading.Event()
    guards, completed = [], []
    task.completed.connect(lambda *args: completed.append(args))
    def read(current):
        guards.append(current)
        started.set()
        assert release.wait(1)
        return "late"
    try:
        task.submit("opaque-key", read, guarded=True)
        assert started.wait(1) and guards[0]()
        task.cancel()
        assert not guards[0]() and not task._timer.isActive()
        release.set()
        wait(lambda: not task._running)
        assert not completed
    finally:
        release.set()
        task.cancel()


def test_quality_trial_does_not_change_search_completion_or_load_media(scene):
    scene.panel.quality_trial_action.trigger()
    assert scene.controller.source_quality_trial_enabled()
    scene.mpv.reset_mock()
    scene.panel.edit_kw.setText("公开测试片名")
    scene.panel._do_search()
    wait(lambda: not scene.panel._film_search._running)
    assert scene.panel.list_results.count() == 1
    assert "正在搜索" not in scene.panel.lbl_status.text()
    scene.mpv.loadfile.assert_not_called()
    assert not scene.window.quality_notice.isVisible()
    scene.panel.quality_trial_action.trigger()


def test_return_home_cancels_deadline_and_preserves_page(movie_panel):
    panel, movie = movie_panel
    panel._film_search._timeout_ms = 100
    started, release = threading.Event(), threading.Event()
    def search(keyword):
        started.set()
        assert release.wait(1)
        return [{"vod_name": "late"}]
    movie.search = search
    try:
        panel.edit_kw.setText("公开测试片名")
        panel._do_search()
        assert started.wait(1)
        panel._show_movie_home()
        panel._film_search._expire()  # A queued old watchdog must not rewrite home.
        QTest.qWait(150)
        assert not panel._film_search._timer.isActive()
        release.set()
        wait(lambda: not panel._film_search._running)
        assert panel.stack_results.currentWidget() is panel.movie_home
        assert "已返回" in panel.lbl_status.text() and "正在搜索" not in panel.lbl_status.text()
        assert panel.list_results.count() == 0
    finally:
        release.set()


def test_manual_search_renders_healthy_third_source_without_autoplay(scene, monkeypatch):
    from types import MethodType
    from app.movie.movie_controller import MovieController
    from tests.unit.test_movie_search_fallback_budget import connection_scenario
    clock = [time.monotonic()]
    isolated, calls = connection_scenario(monkeypatch, clock)
    scene.controller._source_manager = isolated._source_manager
    monkeypatch.setattr(scene.controller, "search", MethodType(MovieController.search, scene.controller))
    scene.mpv.reset_mock()
    scene.panel.edit_kw.setText("公开测试片名")
    scene.panel._do_search()
    wait(lambda: not scene.panel._film_search._running)
    assert len(calls) == 3
    assert scene.panel.list_results.count() == 1
    assert "healthy" in scene.panel.lbl_status.text() and "正在搜索" not in scene.panel.lbl_status.text()
    assert scene.controller._source_manager.get_active_name() == "first"
    scene.mpv.loadfile.assert_not_called()


def test_three_connection_failures_finish_each_generation_and_allow_retry(scene, monkeypatch):
    """Use real controller + Qt delivery; no user config, DB or external HTTP."""
    from types import MethodType, SimpleNamespace
    import requests
    from io import BytesIO
    from urllib3.response import HTTPResponse
    from app.movie import movie_controller as module
    from app.movie.movie_controller import MovieController

    from app.ui.panels import movie_panel as panel_module
    diagnostic = Mock(wraps=panel_module.logger.info)
    monkeypatch.setattr(panel_module.logger, "info", diagnostic)

    sources = [{"name": f"source-{i}", "url": f"https://source-{i}.invalid/cms"}
               for i in range(3)]
    scene.controller._source_manager = SimpleNamespace(
        get_enabled_sources=lambda: sources, get_active_name=lambda: "source-0")
    monkeypatch.setattr(scene.controller, "search", MethodType(MovieController.search, scene.controller))
    calls, finished, heartbeat = [], [], []
    failing = [True]
    def get(url, **kwargs):
        assert QThread.currentThread() != scene.app.thread()
        calls.append(url)
        if failing[0]:
            raise requests.ConnectTimeout("synthetic connect failure")
        response = requests.Response()
        response.status_code = 200
        response.encoding = "utf-8"
        response.raw = HTTPResponse(body=BytesIO(
            b'{"list":[{"vod_id":1,"vod_name":"Public test","vod_play_url":"film$synthetic"}]}'),
            preload_content=False)
        return response
    monkeypatch.setattr(module.requests, "get", get)
    scene.panel._film_search.completed.connect(
        lambda *args: finished.append((args, QThread.currentThread())))
    timer = QTimer()
    timer.timeout.connect(lambda: heartbeat.append(1))
    timer.start(1)
    scene.mpv.reset_mock()
    try:
        for serial in (1, 2, 3):
            scene.panel.edit_kw.setText("public synthetic query")
            scene.panel._do_search()
            wait(lambda: len(finished) == serial)
            assert scene.panel._film_search._serial == serial
            assert not scene.panel._film_search._running
            assert not scene.panel._film_search._timer.isActive()
            assert "搜索未完成" in scene.panel.lbl_status.text()
            assert "正在搜索" not in scene.panel.lbl_status.text()
            assert scene.panel.list_results.count() == 0
            assert finished[-1][0][1:] == ([], None)
            assert finished[-1][1] == scene.app.thread()
            assert scene.controller.last_search_error.count("ConnectTimeout") == 3
            assert len(calls) == serial * 3
        failing[0] = False
        scene.panel._do_search()
        wait(lambda: len(finished) == 4)
        assert "找到" in scene.panel.lbl_status.text()
        assert scene.panel.list_results.count() == 1
        assert len(calls) == 10
        messages = [str(call.args[0]) % call.args[1:] for call in diagnostic.call_args_list]
        received = [m for m in messages if "stage=ui_received" in m]
        settled = [m for m in messages if "stage=ui_settled" in m]
        assert len(received) == len(settled) == 4
        assert sum("outcome=failed" in m for m in settled) == 3
        assert "outcome=results" in settled[-1]
        assert all("public synthetic query" not in m and ".invalid" not in m for m in received + settled)
        assert heartbeat
        scene.mpv.loadfile.assert_not_called()
    finally:
        timer.stop()


def test_active_timeout_jisu_results_reach_gui_and_selected_media_keeps_source(scene, monkeypatch, caplog):
    from io import BytesIO
    import json
    import logging
    from types import MethodType
    from urllib3.response import HTTPResponse
    from PySide6.QtCore import Qt
    from app.movie import movie_controller as module
    from app.movie.movie_controller import MovieController
    from app.ui.panels import movie_panel as panel_module
    from tests.unit.test_movie_search_priority import scenario
    import requests

    isolated, state = scenario()
    scene.controller._source_manager = isolated._source_manager
    monkeypatch.setattr(scene.controller, 'search', MethodType(MovieController.search, scene.controller))
    rows = [
        {'vod_id': 'test-film', 'vod_name': '流浪地球2', 'vod_play_url': '正片$https://synthetic.invalid/film'},
        {'vod_id': 'test-extra', 'vod_name': '流浪地球2：公开测试花絮',
         'vod_play_url': '花絮$https://synthetic.invalid/extra'},
    ]
    calls = []
    def get(url, **kwargs):
        assert QThread.currentThread() != scene.app.thread()
        calls.append(url)
        assert kwargs['params'] == {'ac': 'videolist', 'wd': '流浪地球2'}
        if url != state['sources'][4]['url']:
            raise requests.ConnectTimeout('synthetic')
        response = requests.Response()
        response.status_code = 200
        response.encoding = 'utf-8'
        response.raw = HTTPResponse(body=BytesIO(json.dumps({'list': rows}).encode('utf-8')),
                                    preload_content=False)
        return response
    monkeypatch.setattr(module.requests, 'get', get)
    diagnostic = Mock(wraps=panel_module.logger.info)
    monkeypatch.setattr(panel_module.logger, 'info', diagnostic)
    caplog.set_level(logging.INFO, logger='app.ui.widgets.latest_task')
    scene.mpv.reset_mock()
    returned = threading.Event()
    scene.panel._film_search._returned.connect(lambda *args: returned.set(), Qt.DirectConnection)
    scene.panel.edit_kw.setText('流浪地球2')
    scene.panel._do_search()
    assert returned.wait(2), "Isolated worker must return before testing GUI delivery"
    wait(lambda: not scene.panel._film_search._running)
    assert calls == [state['sources'][0]['url'], state['sources'][4]['url']]
    assert scene.panel.list_results.count() == scene.panel.list_posters.count() == 2
    assert '极速资源' in scene.panel.lbl_status.text()
    assert '正在搜索' not in scene.panel.lbl_status.text()
    assert not scene.panel._film_search._timer.isActive()
    assert state['active'] == '量子资源'
    scene.controller._source_manager._save.assert_not_called()
    scene.mpv.loadfile.assert_not_called()
    for index, expected in enumerate(rows):
        item = scene.panel.list_results.item(index).data(Qt.UserRole)
        assert item['vod_name'] == expected['vod_name']
        assert '流浪地球2' in item['vod_name']
        assert item['_source'] == '极速资源'
        assert item['first_url'] == expected['vod_play_url'].split('$', 1)[1]
    stages = [str(c.args[0]) % c.args[1:] for c in diagnostic.call_args_list]
    assert any('stage=ui_received' in line for line in stages)
    assert any('stage=ui_settled' in line and 'outcome=results' in line for line in stages)
    assert 'stage=emit' in caplog.text and 'stage=delivered' in caplog.text
    # Explicit selection uses that result's data, never a URL from active_source.
    selected = scene.panel.list_results.item(0).data(Qt.UserRole)
    scene.panel._play_item(selected)
    wait(lambda: scene.mpv.loadfile.called)  # Existing embed entry runs on the next Qt turn.
    scene.mpv.loadfile.assert_called_once_with(selected['first_url'])
    assert scene.controller.current_item()['_source'] == '极速资源'
