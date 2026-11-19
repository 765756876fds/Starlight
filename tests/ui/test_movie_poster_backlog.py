"""Large poster batches stay recoverable and viewport-prioritised, with local images."""
import time
import threading

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest

from tests.ui.test_movie_search_posters import (
    images as images, private_cache_never_used as private_cache_never_used, key, wait,
)
from tests.ui.test_movie_voice_window import scene as scene
from tests.ui.test_movie_progressive_ui import screen as screen, begin
from tests.ui.test_movie_progressive_search import cms as cms

pytestmark = pytest.mark.ui


def rows(images, count=160, prefix='batch'):
    return [dict(vod_id=i, vod_name=f'Public {i}', _source='A', _source_id='a',
                 vod_pic=images.url+f'/{prefix}-{i}', first_url=f'http://media.invalid/{i}')
            for i in range(count)]


def test_more_than_120_posters_are_not_lost_at_pending_limit(images, record_property):
    loader = images.make()
    seen = []
    loader.ready.connect(lambda _, k, pix: seen.append(k))
    started = time.monotonic()
    for i in range(160):
        path = f'/image-{i}'
        loader.request(key(images, path, resource=str(i)), images.url+path)
    wait(lambda: len(seen) == 160, seconds=8)
    assert len(set(seen)) == sum(images.calls.values()) == 160
    assert loader.peak_active == images.peak[0] == 2
    assert len(loader._memory) <= loader.MAX_MEMORY
    assert not loader._pending and not loader._queue and not loader._deferred
    record_property('posters_completed', len(seen))
    record_property('all_local_posters_seconds', time.monotonic()-started)


def test_large_wall_results_appear_first_and_all_posters_fill(scene, images, record_property):
    panel = scene.panel
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    values = rows(images)
    started = time.monotonic()
    try:
        panel._on_search_done(values)
        append_time = time.monotonic()-started
        assert panel.list_posters.count() == 160 and sum(images.calls.values()) == 0
        wait(lambda: all(panel.list_posters.item(i).data(Qt.UserRole+83) for i in range(160)), seconds=8)
        assert len(list(panel._search_posters.directory.glob('cms2-*.png'))) == 160
        assert panel._search_posters.peak_active == 2
        assert append_time < .5
        assert ticks and max((b-a for a,b in zip(ticks, ticks[1:])), default=0) < .5
        scene.mpv.loadfile.assert_not_called()
        record_property('rows', 160)
        record_property('initial_rows_render_seconds', append_time)
        record_property('max_gui_gap_ms', max((b-a for a,b in zip(ticks, ticks[1:])), default=0)*1000)
    finally:
        timer.stop()


def test_scroll_visible_posters_jump_a_full_backlog(scene, images):
    panel = scene.panel
    panel.resize(850, 650)
    panel.show()
    values = rows(images, 180, 'slow-scroll')
    panel._on_search_done(values)
    panel.stack_results.setCurrentWidget(panel.list_posters)
    wait(lambda: sum(images.calls.values()) == 2)
    panel.list_posters.scrollToItem(panel.list_posters.item(179))
    QTest.qWait(30)
    panel._prioritise_search_posters()
    queued = list(panel._search_posters._queue)
    assert any(int(job[2].rsplit('-', 1)[1]) >= 170 for job in queued[:10])
    # Unreleased slow requests must remain the only active downloads.
    assert panel._search_posters.peak_active == 2
    images.release.set()
    wait(lambda: panel.list_posters.item(179).data(Qt.UserRole+83), seconds=8)
    assert next(i for i,p in enumerate(images.calls) if p == '/slow-scroll-179') < 110


@pytest.mark.parametrize('action', ['new', 'home', 'close'])
def test_large_backlog_is_invalidated_on_navigation(scene, images, action):
    panel = scene.panel
    panel._on_search_done(rows(images, 160, 'slow-old'))
    wait(lambda: sum(images.calls.values()) == 2)
    old = panel._search_posters._generation
    if action == 'new':
        panel._on_search_done(rows(images, 1, 'fresh'))
    elif action == 'home':
        panel._show_movie_home()
    else:
        panel.close()
    assert panel._search_posters._generation != old
    assert not panel._search_posters._deferred
    assert not panel._poster_overflow
    if action == 'new':
        wait(lambda: panel.list_posters.item(0).data(Qt.UserRole+83), seconds=3)
        assert panel.list_posters.count() == 1
    else:
        wait(lambda: not panel._search_posters._active)
    images.release.set()


def test_backlog_remains_deduplicated_and_generation_scoped(images):
    loader = images.make()
    for i in range(250):
        path = f'/slow-{i}'
        for _ in range(2):
            loader.request(key(images, path, resource=str(i)), images.url+path)
    assert len(loader._queue) <= 100 and len(loader._deferred) <= 2000
    assert len(loader._pending)+len(loader._deferred) == 250
    loader.begin()
    assert not loader._pending and not loader._deferred
    assert not loader._queue


def test_panel_refills_even_when_metadata_cap_is_reached(scene, images, monkeypatch):
    panel = scene.panel
    monkeypatch.setattr(panel._search_posters, 'MAX_PENDING', 4)
    monkeypatch.setattr(panel._search_posters, 'MAX_DEFERRED', 3)
    panel._on_search_done(rows(images, 140, 'overflow'))
    assert panel._poster_overflow
    wait(lambda: all(panel.list_posters.item(i).data(Qt.UserRole+83) for i in range(140)), seconds=8)
    assert not panel._poster_overflow and not panel._search_posters._deferred
    assert sum(images.calls.values()) == 140


def test_large_slow_first_fast_later_cms_batches_keep_poster_generation(screen, cms, images):
    slow = rows(images, 125, 'slow-first')
    fast = rows(images, 35, 'later')
    for item in fast:
        item['vod_id'] += 500
    cms.modes['/s0'] = dict(rows=slow)
    later = threading.Event()
    cms.modes['/s1'] = dict(rows=fast, wait=later)
    cms.modes['/s2'] = dict(rows=[])
    panel = screen.panel
    begin(screen)
    generation = panel._search_posters._generation
    wait(lambda: panel.list_posters.count() == 125, seconds=5)
    later.set()
    wait(lambda: panel.list_posters.count() == 160, seconds=8)
    assert panel._search_posters._generation == generation
    assert not any(panel.list_posters.item(i).data(Qt.UserRole+83) for i in range(125))
    panel.stack_results.setCurrentWidget(panel.list_posters)
    panel.show()
    panel.list_posters.scrollToItem(panel.list_posters.item(159))
    panel._prioritise_search_posters()
    images.release.set()
    wait(lambda: all(panel.list_posters.item(i).data(Qt.UserRole+83) for i in range(160)), seconds=8)
    assert panel._search_posters._generation == generation
    assert panel._search_posters.peak_active == 2
    screen.mpv.loadfile.assert_not_called()
