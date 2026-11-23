"""Bounded coverage phases, using controlled clocks and owned local CMS children."""
import json

import pytest
from PySide6.QtCore import QObject, QCoreApplication, QEvent, Signal
from PySide6.QtWidgets import QApplication

from app.movie.progressive_search import ProgressiveMovieSearch
from app.movie.source_manager import MovieSourceManager
from tests.ui.test_movie_progressive_search import cms as cms, wait
from tests.ui.test_movie_progressive_ui import screen as screen, begin
from tests.ui.test_movie_voice_window import scene as scene

pytestmark = pytest.mark.ui


class Pool(QObject):
    returned = Signal(object, object)
    available = Signal()
    MAX_ACTIVE = 2

    def __init__(self):
        super().__init__()
        self.active, self.calls, self.cancelled = {}, [], set()
        self.peak = 0

    def start(self, token, payload, timeout_ms):
        assert len(self.active) < 2
        self.active[token] = payload
        self.calls.append((token, payload, timeout_ms))
        self.peak = max(self.peak, len(self.active))
        return True

    def finish(self, token, status='empty', error=None):
        self.active.pop(token)
        if token not in self.cancelled:
            self.returned.emit(token, dict(status=status, items=[], error=error))
        self.available.emit()

    def cancel(self):
        self.cancelled.update(self.active)

    def shutdown(self):
        self.cancel()
        self.active.clear()


@pytest.fixture
def controlled(tmp_path):
    app = QApplication.instance() or QApplication([])
    services = []

    def make(count=9):
        path = tmp_path / f'config-{len(services)}.json'
        path.write_text(json.dumps({'movie': {'sources': [dict(name=f's{i}', enabled=True,
            url=f'http://127.0.0.1/{i}') for i in range(count)], 'active_source': 's0'}}))
        manager = MovieSourceManager(str(path))
        clock = [0.]
        pool = Pool()
        service = ProgressiveMovieSearch(pool=pool, clock=lambda: clock[0])
        service.configure(manager, lambda: False)
        service.probe_timer.stop()
        services.append(service)
        return service, pool, clock, manager

    yield make
    for service in services:
        service.close()
        service.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_nine_slow_sources_get_one_attempt_after_fast_phase(controlled, record_property):
    service, pool, clock, _ = controlled()
    batches, phases, done = [], [], []
    service.batch.connect(lambda _, r: batches.append(r))
    service.progress.connect(lambda _, r: phases.append(r))
    service.completed.connect(lambda _, r: done.append(r))
    service.start('Public')
    assert service.request['budget_ms'] == 52000
    for wave in range(5):
        service._pump()
        clock[0] += 10
        if clock[0] == 30:
            service._supplement()
            assert phases and not done
        for token in tuple(pool.active):
            pool.finish(token, 'failed', 'ReadTimeout')
    service._pump()
    assert len(pool.calls) == len({p['url'] for _, p, _ in pool.calls}) == 9
    assert pool.peak == 2 and len(batches) == 9
    assert done[0]['reason'] == 'complete'
    assert done[0]['attempted'] == done[0]['done'] == done[0]['timed_out'] == 9
    assert done[0]['unattempted'] == done[0]['in_flight'] == done[0]['errors'] == 0
    record_property('nine_source_absolute_budget_ms', 52000)


def test_absolute_cap_reports_unattempted_separately_from_timeout(controlled):
    service, pool, clock, _ = controlled()
    service.BUDGET_MS = 25000
    service.FIRST_BATCH_MS = 1000
    done = []
    service.completed.connect(lambda _, r: done.append(r))
    service.start('Public')
    for _ in range(2):
        service._pump()
        clock[0] += 10
        for token in tuple(pool.active):
            pool.finish(token)
    service._pump()
    clock[0] = 25
    service._expire()
    result = done[0]
    assert result['reason'] == 'budget'
    assert (result['done'], result['attempted'], result['unattempted'], result['timed_out']) == (4, 6, 3, 2)
    assert len(pool.cancelled) == 2


def test_manual_choice_and_health_order_still_lead(controlled):
    service, pool, _, manager = controlled()
    sources = service._sources()
    service.health.observe(sources[5]['identity'], dict(status='ok', elapsed_ms=1))
    manager.set_active('s8')
    service.start('Public')
    service._pump()
    assert [p['url'].rsplit('/', 1)[1] for _, p, _ in pool.calls] == ['8', '5']


@pytest.mark.parametrize('action', ['new', 'detach', 'close'])
def test_supplementary_phase_cancellation_does_not_revive_old_goal(controlled, action):
    service, pool, _, _ = controlled()
    seen = []
    service.batch.connect(lambda serial, _: seen.append(serial))
    old = service.start('old')
    service._pump()
    old_tokens = tuple(pool.active)
    if action == 'new':
        current = service.start('new')
        assert current != old
        service._pump()
        assert len(pool.calls) == 2  # Old jobs still own both slots.
    else:
        getattr(service, action)()
    for token in old_tokens:
        if token in pool.active:
            pool.finish(token, 'ok')
    assert old not in seen
    if action == 'new':
        service._pump()
        assert len(pool.calls) == 4 and pool.peak == 2
    else:
        assert not service.phase_timer.isActive()


def test_real_nine_cms_sources_finish_once_with_two_process_slots(cms, record_property):
    service, _, _ = cms.make(9)
    service.FIRST_BATCH_MS = 500
    service.SOURCE_MS = 3000
    for i in range(9):
        cms.modes[f'/s{i}'] = dict(delay=.1)
    seen, done = [], []
    service.batch.connect(lambda _, r: seen.append(r))
    service.completed.connect(lambda _, r: done.append(r))
    service.start('Public')
    wait(lambda: bool(done), seconds=15)
    assert len(cms.calls) == len(seen) == 9
    assert done[0]['reason'] == 'complete' and done[0]['unattempted'] == 0
    assert service.pool.peak == 2 and cms.peak[0] <= 2
    assert all(c[1]['ac'] == ['videolist'] and 'pg' not in c[1] for c in cms.calls)
    record_property('real_local_sources_attempted', len(cms.calls))


def test_panel_watchdog_uses_the_absolute_supplementary_deadline(screen, cms):
    service = screen.service
    service.SOURCE_MS = 20000
    begin(screen)
    assert service.request['budget_ms'] == 42000
    assert screen.panel._progressive_ui_timer.interval() == 47000
    serial = screen.panel._progressive_serial
    screen.panel._progressive_progress(serial, dict(phase='supplement'))
    assert screen.panel.lbl_status.text() == '正在搜索…'
    screen.panel.list_posters.addItem('已有影片')
    screen.panel._progressive_progress(serial, dict(phase='supplement'))
    assert screen.panel.lbl_status.text() == '已找到 1 部，正在加载更多…'
    assert screen.panel._progressive_ui_timer.interval() == 47000
    screen.panel._freeze_progressive()
    screen.panel._progressive_progress(serial, dict(phase='supplement'))
    assert not screen.panel._progressive_ui_timer.isActive()


@pytest.mark.parametrize('action', ['leave_page', 'voice_playback'])
def test_leaving_search_or_voice_playback_cancels_supplement_immediately(screen, cms, action):
    import threading
    gate = threading.Event()
    cms.modes['/s0'] = cms.modes['/s1'] = dict(wait=gate)
    panel = screen.panel
    panel.show()
    begin(screen)
    wait(lambda: len(screen.service.pool.active) == 2)
    if action == 'leave_page':
        panel.hide()
    else:
        assert screen.controller.play(dict(vod_name='Public', first_url='https://media.invalid/public'))
        panel._on_voice_playback_ready(lambda: True)
    assert panel._progressive_serial is None
    assert not screen.service.request
    wait(lambda: not screen.service.pool.active)
    gate.set()


def test_nine_slow_sources_all_obtain_a_turn_before_final_cutoff(controlled):
    service, pool, clock, _ = controlled()
    service.start('Public')
    for _ in range(5):
        if service.request is None:
            break
        service._pump()
        clock[0] += 10
        for token in tuple(pool.active):
            pool.finish(token)
    service._pump()
    assert len(pool.calls) == 9, 'Every configured source must obtain its one bounded turn'
    assert len({p['url'] for _, p, _ in pool.calls}) == 9


@pytest.mark.parametrize('error, expected', [('ReadTimeoutError', 1), ('InvalidCMS', 0)])
def test_transport_timeout_is_separate_from_protocol_failure(controlled, error, expected):
    service, pool, _, _ = controlled(2)
    service.start('Public')
    service._pump()
    pool.finish(next(iter(pool.active)), 'failed', error)
    counts = service._counts(service.request)
    assert counts['timed_out'] == expected
    assert counts['errors'] == 1-expected
    assert counts['failed'] == 1
