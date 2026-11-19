"""Real panel assembly/signals and owned HTTP children, mock playback only."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtTest import QTest

from app.movie.movie_controller import MovieController
from app.movie.progressive_search import shared_search_service as real_shared_search_service
from app.ui.panels.movie_panel import MoviePanel
from tests.ui.test_movie_progressive_search import cms as cms, wait
from tests.ui.test_movie_search_posters import images as images
from tests.ui.test_movie_voice_window import scene as scene

pytestmark = pytest.mark.ui


@pytest.fixture
def screen(scene, cms, tmp_path, monkeypatch):
    service, manager, path = cms.make(3)
    monkeypatch.setattr('app.movie.progressive_search.shared_search_service', lambda: service)
    from app.ui.panels import movie_panel
    monkeypatch.setattr(movie_panel, '_POSTER_DIR', tmp_path/'posters')
    ctrl = MovieController(config={'app.data_dir': str(tmp_path/'film'), 'movie.embed': True},
                           source_manager=manager)
    ctrl.film_memory = Mock()
    panel = MoviePanel(movie=ctrl)
    panel._timer.stop()
    panel._video_win._timer.stop()
    service.probe_timer.stop()
    assert panel._progressive_search is service
    scene.mpv.reset_mock()
    yield SimpleNamespace(panel=panel, controller=ctrl, service=service, mpv=scene.mpv,
                          path=path, app=scene.app)
    for timer in panel.findChildren(QTimer):
        timer.stop()
    panel._on_app_quit()
    panel._video_win.close()
    panel.close()
    wait(lambda: not service.pool.active)
    wait(lambda: not panel._film_search._running)
    # This fixture owns an extra panel in addition to scene's existing panel.
    # Dispose it here, while those wrappers are strongly referenced, rather
    # than leaving two native video windows for the general snapshot teardown.
    panel._video_win.deleteLater()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    scene.app.processEvents()


def begin(screen, keyword='Public'):
    screen.panel.edit_kw.setText(keyword)
    screen.panel.btn_search.click()


def test_fast_rows_append_preserving_selection_posters_and_gui(screen, cms, record_property):
    gate = threading.Event()
    cms.modes['/s0'] = dict(wait=gate, rows=[dict(vod_id=i, vod_name=f'Public {i}', vod_pic='',
                                             vod_play_url=f'正片$https://video.invalid/{i}') for i in range(20)])
    cms.modes['/s1'] = dict(delay=1.6, rows=[dict(vod_id=i, vod_name=f'Fast {i}', vod_pic='',
                                     vod_play_url=f'正片$https://video.invalid/fast-{i}') for i in range(2)])
    # The third source stays pending too, so first-batch assertions aren't a race.
    cms.modes['/s2'] = dict(wait=gate, rows=[])
    panel = screen.panel
    starts = Mock(wraps=panel._search_posters.begin)
    panel._search_posters.begin = starts
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    before = time.monotonic()
    begin(screen)
    wait(lambda: panel.list_posters.count() == 2)
    first_cost = time.monotonic()-before
    assert '正在补充' in panel.lbl_status.text()
    selected = panel.list_posters.item(0)
    panel.list_posters.setCurrentItem(selected)
    generation = panel._search_posters._generation
    gate.set()
    wait(lambda: '搜索完成' in panel.lbl_status.text())
    assert panel.list_posters.count() == panel.list_results.count() == 22
    assert panel.list_posters.currentItem() is selected
    assert panel._search_posters._generation == generation and starts.call_count == 1
    assert '极速资源' in panel.list_results.item(0).text()
    screen.mpv.loadfile.assert_not_called()
    screen.controller.film_memory.record_action.assert_called_once_with('search', 'Public')
    timer.stop()
    assert max(b-a for a,b in zip(ticks,ticks[1:])) < .5
    record_property('local_first_batch_seconds', first_cost)
    record_property('max_gui_tick_gap_ms', max(b-a for a,b in zip(ticks,ticks[1:]))*1000)


@pytest.mark.parametrize('kind', ['home', 'clear', 'close', 'play', 'new_keyword', 'external_play'])
def test_late_batch_cannot_replace_new_page_or_playing_selection(screen, cms, kind):
    gate = threading.Event()
    cms.modes['/s0'] = dict(wait=gate)
    cms.modes['/s2'] = dict(wait=gate)
    panel = screen.panel
    begin(screen, 'old')
    wait(lambda: panel.list_posters.count() == 1)
    old_serial = panel._progressive_serial
    item = panel.list_posters.item(0)
    payload = dict(item.data(Qt.UserRole))
    if kind == 'home':
        panel._show_movie_home()
    elif kind == 'clear':
        panel._do_clear()
    elif kind == 'close':
        panel.close()
    elif kind == 'play':
        panel._on_item_activated(item)
        wait(lambda: screen.mpv.loadfile.called)
        screen.mpv.loadfile.assert_called_once_with(payload['first_url'])
    elif kind == 'external_play':
        screen.controller._playback_gen += 1
    else:
        cms.modes.clear()
        begin(screen, 'new')
        wait(lambda: '搜索完成' in panel.lbl_status.text())
    before = panel.list_posters.count()
    panel._progressive_batch(old_serial, dict(items=[dict(vod_id='late', vod_name='Late')],
                                             done=3, total=3, failed=0))
    assert panel.list_posters.count() == before
    gate.set()


@pytest.mark.parametrize('mode,expected', [({'status': 503}, '所有来源搜索失败'),
                                         ({'rows': []}, '没有匹配影片')])
def test_all_finished_settles_immediately_and_retry_works(screen, cms, mode, expected):
    for path in ('/s0','/s1','/s2'):
        cms.modes[path] = mode
    begin(screen)
    wait(lambda: expected in screen.panel.lbl_status.text())
    assert screen.service.request is None
    assert '正在搜索' not in screen.panel.lbl_status.text()
    cms.modes.clear()
    begin(screen, 'retry')
    wait(lambda: '搜索完成' in screen.panel.lbl_status.text())
    assert screen.panel.list_posters.count() == 3


def test_background_probe_is_deferred_during_playback_and_never_downloads_video(screen, cms):
    screen.controller._current = {'vod_id': 'owned-test'}
    screen.service.probe()
    QTest.qWait(80)
    assert not cms.calls
    screen.controller._current = None
    screen.service.probe()
    wait(lambda: bool(cms.calls))
    assert len(screen.service.pool.active) <= 1
    assert all(c[0].startswith('/s') for c in cms.calls)


def test_shared_pool_and_frozen_safe_legacy_fallback(screen, monkeypatch):
    import sys
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert real_shared_search_service() is None
    monkeypatch.setattr(sys, 'frozen', False)
    previous = getattr(screen.app, '_movie_progressive_search', None)
    service = real_shared_search_service()
    assert service is real_shared_search_service() and service.pool.MAX_ACTIVE == 2
    if previous is None:
        service.close()
        del screen.app._movie_progressive_search


def test_ui_watchdog_preserves_partial_results_and_ignores_late_batch(screen, cms):
    gate = threading.Event()
    cms.modes['/s0'] = cms.modes['/s2'] = dict(wait=gate)
    begin(screen)
    wait(lambda: screen.panel.list_posters.count() == 1)
    serial = screen.panel._progressive_serial
    screen.panel._progressive_ui_timeout()
    assert '搜索超时' in screen.panel.lbl_status.text()
    screen.panel._progressive_batch(serial, dict(items=[dict(vod_id='late')], done=3, total=3, failed=0))
    assert screen.panel.list_posters.count() == 1
    wait(lambda: not screen.service.pool.active)
    gate.set()


def test_actual_posters_continue_across_incremental_cms_batches(screen, cms, images):
    gate = threading.Event()
    cms.modes['/s0'] = dict(wait=gate, rows=[dict(vod_id=2, vod_name='Second',
                                               vod_pic=images.url+'/ok')])
    cms.modes['/s1'] = dict(rows=[dict(vod_id=1, vod_name='First',
                                     vod_pic=images.url+'/slow')])
    cms.modes['/s2'] = dict(wait=gate, rows=[])
    panel = screen.panel
    begin(screen)
    wait(lambda: panel.list_posters.count() == 1 and images.calls['/slow'] == 1)
    first = panel.list_posters.item(0)
    first_key = first.data(Qt.UserRole+82)
    generation = panel._search_posters._generation
    gate.set()
    wait(lambda: panel.list_posters.count() == 2)
    second_key = panel.list_posters.item(1).data(Qt.UserRole+82)
    wait(lambda: panel._search_posters.cached(second_key) is not None)
    assert panel._search_posters.cached(first_key) is None
    assert panel.list_posters.item(0) is first
    assert panel._search_posters._generation == generation
    assert images.calls['/slow'] == images.calls['/ok'] == 1
    assert panel._search_posters.peak_active <= 2
    images.release.set()
    wait(lambda: panel._search_posters.cached(first_key) is not None)
    screen.mpv.loadfile.assert_not_called()
