"""Local synthetic CMS only; authoritative real process pool, no user databases."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.movie.progressive_search import ProgressiveMovieSearch, SearchHealth, work_identity
from app.movie.source_manager import MovieSourceManager

pytestmark = pytest.mark.ui


def wait(predicate, seconds=4):
    end = time.monotonic()+seconds
    while not predicate() and time.monotonic() < end:
        QTest.qWait(10)
    assert predicate(), "local progressive state was not reached"


@pytest.fixture
def cms(tmp_path):
    app = QApplication.instance() or QApplication([])
    modes, calls, lock = {}, [], threading.Lock()
    active, peak = [0], [0]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            from urllib.parse import urlsplit, parse_qs
            address = urlsplit(self.path)
            with lock:
                calls.append((address.path, parse_qs(address.query), dict(self.headers)))
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            mode = modes.get(address.path, {})
            try:
                if mode.get("wait"):
                    mode["wait"].wait(6)
                time.sleep(mode.get("delay", 0))
                rows = mode.get("rows", [dict(vod_id=1, vod_name="Public test", vod_year="2024",
                                               vod_pic="", vod_play_url="正片$https://video.invalid/film.m3u8")])
                body = mode.get("body", json.dumps({"list": rows}, ensure_ascii=False).encode())
                self.send_response(mode.get("status", 200))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ConnectionError):
                pass
            finally:
                with lock:
                    active[0] -= 1
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    services = []
    def make(count=4):
        sources = [dict(name="极速资源" if i == 1 else f"source-{i}",
                        url=f"http://127.0.0.1:{server.server_port}/s{i}", enabled=True)
                   for i in range(count)]
        path = tmp_path/f"sources-{len(services)}.yaml"
        path.write_text(json.dumps({"movie": {"sources": sources, "active_source": "source-0"}}))
        manager = MovieSourceManager(str(path))
        service = ProgressiveMovieSearch()
        service.configure(manager, lambda: True)
        service.probe_timer.stop()
        services.append(service)
        return service, manager, path
    s = SimpleNamespace(app=app, modes=modes, calls=calls, peak=peak, make=make)
    yield s
    for mode in modes.values():
        if mode.get("wait"):
            mode["wait"].set()
    for service in services:
        service.close()
        wait(lambda: not service.pool.active)
        service.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    server.shutdown()
    server.server_close()
    thread.join(2)
    assert not thread.is_alive() and active[0] == 0


def test_fast_two_then_slow_twenty_and_all_sources_participate(cms, record_property):
    gate = threading.Event()
    cms.modes['/s0'] = dict(wait=gate, rows=[dict(vod_id=i, vod_name=f"Public {i}") for i in range(20)])
    cms.modes['/s1'] = dict(rows=[dict(vod_id=i, vod_name=f"Fast {i}") for i in range(2)])
    service, _, path = cms.make(5)
    original = path.read_bytes()
    batches, done = [], []
    service.batch.connect(lambda _, value: batches.append(value))
    service.completed.connect(lambda _, value: done.append(value))
    service.start("流浪地球2")
    wait(lambda: any(b["source_name"] == "极速资源" for b in batches))
    assert len(batches[0]["items"]) == 2 and not done
    wait(lambda: len(cms.calls) >= 2)  # Independent child startup can finish out of order.
    assert {c[0] for c in cms.calls[:2]} == {'/s0', '/s1'}
    gate.set()
    wait(lambda: done)
    assert sum(len(b['items']) for b in batches) == 25
    assert len(cms.calls) == 5 and service.pool.peak == 2 and cms.peak[0] <= 2
    assert all(c[1] == {"ac": ["videolist"], "wd": ["流浪地球2"]} for c in cms.calls)
    assert all(c[2].get('User-Agent') for c in cms.calls)
    assert path.read_bytes() == original
    record_property("pool_peak", service.pool.peak)


@pytest.mark.parametrize("mode,status", [({"rows": []}, "empty"), ({"body": b"bad"}, "failed"),
                                         ({"status": 503}, "failed"), ({"body": b"x"*2_000_001}, "failed")])
def test_empty_invalid_http_and_size_are_distinct(cms, mode, status):
    cms.modes['/s0'] = mode
    service, _, _ = cms.make(1)
    batches, done = [], []
    service.batch.connect(lambda _, value: batches.append(value))
    service.completed.connect(lambda _, value: done.append(value))
    service.start("Public")
    wait(lambda: done)
    assert batches[0]['status'] == status and done[0]['done'] == 1


def test_new_generation_cancels_old_and_keeps_two_slots(cms):
    gate = threading.Event()
    cms.modes['/s0'] = cms.modes['/s1'] = dict(wait=gate)
    service, _, _ = cms.make(2)
    batches = []
    service.batch.connect(lambda serial, value: batches.append((serial, value)))
    first = service.start("old")
    wait(lambda: len(cms.calls) == 2)
    second = service.start("new")
    assert second > first
    cms.modes.clear()
    wait(lambda: len(batches) == 2)
    assert {b[0] for b in batches} == {second} and service.pool.peak == 2
    gate.set()


def test_absolute_budget_reaps_slow_sources_and_preserves_partial(cms):
    gate = threading.Event()
    cms.modes['/s0'] = dict(wait=gate)
    service, _, _ = cms.make(3)
    service.BUDGET_MS = 2000
    batches, done = [], []
    service.batch.connect(lambda _, value: batches.append(value))
    service.completed.connect(lambda _, value: done.append(value))
    service.start("Public")
    wait(lambda: done)
    assert done[0]['reason'] == 'budget' and done[0]['shown'] > 0
    wait(lambda: not service.pool.active)
    assert batches and not service.request
    gate.set()


def test_duplicate_endpoints_and_same_source_rows_are_deduplicated(cms):
    row = dict(vod_id=5, vod_name="Same", vod_play_url="正片$https://video.invalid/1")
    cms.modes['/s0'] = dict(rows=[row, row])
    cms.modes['/s1'] = dict(rows=[row])
    service, manager, _ = cms.make(2)
    manager._sources.append(dict(manager._sources[0], name='alias'))
    batches, done = [], []
    service.batch.connect(lambda _, value: batches.extend(value['items']))
    service.completed.connect(lambda *_: done.append(True))
    service.start("Public")
    wait(lambda: done)
    assert len(cms.calls) == 2 and len(batches) == 2
    assert batches[0]['_source_id'] != batches[1]['_source_id']
    assert all(b['play_url'] == row['vod_play_url'] for b in batches)


def test_external_identity_caps_three_but_keeps_internal_alternatives(cms):
    row = dict(vod_id=1, vod_name='Same', vod_year='2024', type_name='电影',
               imdb_id='tt1234567', vod_play_url='正片$https://video.invalid/1')
    for i in range(5):
        cms.modes[f'/s{i}'] = dict(rows=[row])
    service, _, _ = cms.make(5)
    batches, done = [], []
    service.batch.connect(lambda _, value: batches.extend(value['items']))
    service.completed.connect(lambda *_: done.append(True))
    service.start('Public')
    wait(lambda: done)
    assert len(batches) == 3 and len(service.alternatives) == 2
    assert work_identity(dict(row, vod_year='2023')) != work_identity(row)
    assert work_identity(dict(row, imdb_id='')) is None


def test_health_probe_is_search_only_and_shared_with_user_search(cms):
    service, _, path = cms.make(3)
    original = path.read_bytes()
    service.probe()
    wait(lambda: len(cms.calls) >= 1)
    assert len(service.pool.active) <= 1
    seen = []
    service.completed.connect(lambda *_: seen.append(True))
    service.start('Public')
    wait(lambda: seen)
    assert service.pool.peak <= 2
    assert all(c[0].startswith('/s') for c in cms.calls)  # Never fetch play URLs/HLS.
    assert path.read_bytes() == original and not service._jobs


def test_active_playback_defers_probes_and_default_unknown_is_not_failed(cms):
    service, _, _ = cms.make(2)
    service.idle = lambda: False
    service.probe()
    QTest.qWait(80)
    assert not cms.calls and not service.pool.active
    clock = [100.]
    health = SearchHealth(lambda: clock[0])
    assert health.rank('unknown')[0] == 1
    health.observe('good', dict(status='ok', elapsed_ms=1500))
    health.observe('bad', dict(status='failed', elapsed_ms=100))
    assert health.rank('good') < health.rank('unknown') < health.rank('bad')
    clock[0] += 601
    assert health.rank('good') == health.rank('unknown')


def test_source_revision_change_discards_late_batch(cms):
    gate = threading.Event()
    cms.modes['/s0'] = dict(wait=gate)
    service, manager, _ = cms.make(1)
    batches, done = [], []
    service.batch.connect(lambda *_: batches.append(True))
    service.completed.connect(lambda _, value: done.append(value))
    service.start('Public')
    wait(lambda: cms.calls)
    manager.set_active('source-0')
    gate.set()
    wait(lambda: done)
    assert not batches and done[0]['reason'] == 'changed'


def test_health_order_respects_manual_selection_and_all_failed_uses_baseline(cms):
    service, manager, _ = cms.make(3)
    sources = service._sources()
    assert service._ordered(sources)[0]['name'] == 'source-0'
    for source in sources:
        service.health.observe(source['identity'], dict(status='failed', elapsed_ms=10))
    assert service._ordered(sources)[0]['name'] == 'source-0'
    service.health.observe(sources[1]['identity'], dict(status='ok', elapsed_ms=1000))
    assert service._ordered(sources)[0]['name'] == '极速资源'
    manager.set_active('source-0')
    assert service._ordered(sources)[0]['name'] == 'source-0'
    manager.set_active('source-0')
    assert service._ordered(sources)[0]['name'] == 'source-0'


def test_real_http_children_memory_and_cancel_reap_are_bounded(cms, record_property):
    import psutil
    gate = threading.Event()
    cms.modes['/s0'] = cms.modes['/s1'] = dict(wait=gate)
    service, _, _ = cms.make(2)
    service.start('Public')
    wait(lambda: len(cms.calls) == 2)
    pids = [state['process'].processId() for state in service.pool.active.values()]
    assert len(pids) == 2
    pids += [child.pid for pid in tuple(pids) for child in psutil.Process(pid).children(recursive=True)]
    memory = [psutil.Process(pid).memory_info().rss for pid in pids]
    record_property('owned_process_names', [psutil.Process(pid).name() for pid in pids])
    started = time.monotonic()
    service.invalidate()
    wait(lambda: not service.pool.active)
    assert all(not psutil.pid_exists(pid) for pid in pids)
    record_property('request_children_rss_bytes', memory)
    record_property('cancel_real_http_children_seconds', time.monotonic()-started)
    gate.set()
