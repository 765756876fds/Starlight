"""Generated pictures and loopback HTTP only, isolated caches and movie databases."""
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest
from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QThread, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.ui.widgets.movie_search_posters import SearchPosterLoader, poster_key
from tests.ui.test_movie_voice_window import scene as scene

pytestmark = pytest.mark.ui


def wait(predicate, seconds=2):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        QTest.qWait(10)
    assert predicate(), 'Bounded local image state was not reached'


@pytest.fixture(autouse=True)
def private_cache_never_used(tmp_path, monkeypatch):
    from app.ui.panels import movie_panel
    monkeypatch.setattr(movie_panel, '_POSTER_DIR', tmp_path / 'posters')


@pytest.fixture
def images(tmp_path):
    app = QApplication.instance() or QApplication([])
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
    image = QImage(16, 24, QImage.Format_RGB32)
    image.fill(QColor('#369ad5'))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    png = bytes(buffer.data())
    calls = Counter()
    release = threading.Event()
    lock = threading.Lock()
    active, peak = [0], [0]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            path = self.path
            with lock:
                calls[path] += 1
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                body = b'<html>error page</html>' if path == '/html' else b'not an image' if path == '/broken' else png
                self.send_response(302 if path in ('/redirect', '/loop') else 403 if path == '/forbidden' else 200)
                if path in ('/redirect', '/loop'):
                    self.send_header('Location', '/ok' if path == '/redirect' else '/loop')
                self.send_header('Content-Type', 'text/html' if path == '/html' else 'image/png')
                if path != '/no-length':
                    self.send_header('Content-Length', str(100_000 if path == '/large' else len(body)))
                else:
                    self.send_header('Connection', 'close')
                    body = b'x' * 4000
                self.end_headers()
                if path.startswith('/slow'):
                    release.wait(3)
                if path == '/drip':
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if release.wait(.02):
                            break
                else:
                    self.wfile.write(body)
            except (ConnectionError, OSError):
                pass
            finally:
                with lock:
                    active[0] -= 1
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    loaders = []
    def make(**kwargs):
        loader = SearchPosterLoader(tmp_path / 'search-v2', **kwargs)
        loaders.append(loader)
        return loader
    class State:
        url = f'http://127.0.0.1:{server.server_port}'
    s = State()
    s.app, s.calls, s.release, s.peak, s.make, s.png = app, calls, release, peak, make, png
    s.active = active
    def drain_handlers():
        for handler in tuple(server._threads):
            handler.join(1)
            assert not handler.is_alive(), 'owned HTTP handler did not finish'
        server._threads.reap()
    s.drain_handlers = drain_handlers
    yield s
    release.set()
    for loader in loaders:
        loader.close()
        wait(lambda: not loader._active)
        loader.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    server.shutdown()
    server.server_close()
    thread.join(2)
    assert not thread.is_alive()
    assert active[0] == 0, 'test HTTP handlers must finish before their fixture exits'


def key(images, path, source='source', resource='1'):
    return poster_key(source, resource, images.url + path)


def test_slow_first_fast_second_is_incremental_and_ui_owned(images, record_property):
    loader = images.make()
    seen, ticks = [], []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(5)
    started = time.monotonic()
    loader.ready.connect(lambda generation, k, pix: seen.append((k, QThread.currentThread())))
    loader.request(key(images, '/slow'), images.url + '/slow')
    loader.request(key(images, '/ok'), images.url + '/ok')
    wait(lambda: bool(seen))
    assert seen == [(key(images, '/ok'), images.app.thread())]
    assert loader.peak_active == 2 and images.peak[0] <= 2
    images.release.set()
    wait(lambda: len(seen) == 2)
    assert seen[1][0] == key(images, '/slow')
    wait(lambda: len(ticks) >= 3)
    timer.stop()
    record_property('peak_network_requests', loader.peak_active)
    record_property('both_ready_seconds', time.monotonic() - started)
    record_property('max_gui_tick_gap_ms', max(b-a for a,b in zip(ticks,ticks[1:]))*1000)
    assert max(b-a for a,b in zip(ticks,ticks[1:])) < .5


def test_many_jobs_never_exceed_two_and_new_batch_uses_same_limit(images):
    loader = images.make()
    for i in range(6):
        loader.request(key(images, f'/slow{i}'), images.url + f'/slow{i}')
    wait(lambda: sum(images.calls.values()) == 2)
    assert len(loader._active) == 2
    loader.begin()
    images.release.set()
    for i in range(6):
        loader.request(key(images, f'/new{i}'), images.url + f'/new{i}')
    wait(lambda: not loader._active and not loader._queue)
    assert loader.peak_active == 2
    assert all(images.calls[f'/slow{i}'] == 0 for i in range(2, 6))


@pytest.mark.parametrize('path', ['/html', '/broken', '/forbidden', '/large'])
def test_invalid_picture_never_enters_disk_or_memory_and_other_job_succeeds(images, path):
    loader = images.make()
    if path == '/large':
        loader.MAX_BYTES = 1000
    bad, good = key(images, path), key(images, '/ok')
    loader.request(bad, images.url + path)
    loader.request(good, images.url + '/ok')
    wait(lambda: not loader._active)
    assert loader.cached(bad) is None and not loader._path(bad).exists()
    assert loader.cached(good) is not None and loader._path(good).exists()


def test_dimension_cap_rejects_decodable_image_before_cache(images):
    loader = images.make()
    loader.MAX_PIXELS = 100
    k = key(images, '/ok')
    loader.request(k, images.url + '/ok')
    wait(lambda: not loader._active)
    assert loader.cached(k) is None and not loader._path(k).exists()


def test_absolute_deadline_defeats_continuously_dripping_response(images):
    loader = images.make()
    loader.TOTAL_MS = 150
    k = key(images, '/drip')
    started = time.monotonic()
    loader.request(k, images.url + '/drip')
    wait(lambda: not loader._active, seconds=.7)
    assert time.monotonic() - started < .7
    assert loader.cached(k) is None and k in loader._failures
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not loader._manager.findChildren(QNetworkReply)


def test_same_identity_is_downloaded_once_and_cache_survives_new_loader(images):
    loader = images.make()
    k = key(images, '/ok')
    for _ in range(4):
        loader.request(k, images.url + '/ok')
    wait(lambda: loader.cached(k) is not None)
    assert images.calls['/ok'] == 1
    fresh = images.make()
    assert fresh.cached(k) is not None
    fresh.request(k, images.url + '/ok')
    QTest.qWait(30)
    assert images.calls['/ok'] == 1


def test_failure_cooldown_is_temporary_and_not_extended_by_display(images):
    clock = [0.]
    loader = images.make(clock=lambda: clock[0])
    k = key(images, '/html')
    loader.request(k, images.url + '/html')
    wait(lambda: not loader._active)
    until = loader._failures[k]
    for _ in range(4):
        loader.request(k, images.url + '/html')
    assert images.calls['/html'] == 1 and loader._failures[k] == until
    clock[0] = until + 1
    loader.request(k, images.url + '/html')
    wait(lambda: images.calls['/html'] == 2 and not loader._active)


def test_source_resource_and_address_all_participate_in_cache_identity(images):
    loader = images.make()
    a = key(images, '/ok', source='A')
    b = key(images, '/ok', source='B')
    assert a != b and a != key(images, '/other', source='A')
    loader.request(a, images.url + '/ok')
    wait(lambda: loader.cached(a) is not None)
    assert loader.cached(b) is None
    legacy = loader.directory.parent / '1.jpg'
    legacy.write_bytes(b'unassigned legacy bytes')
    assert loader.cached(b) is None and legacy.read_bytes() == b'unassigned legacy bytes'


def test_corrupt_new_cache_can_retry_without_deleting_legacy_files(images):
    loader = images.make()
    loader.directory.mkdir()
    k = key(images, '/ok')
    loader._path(k).write_bytes(b'broken png')
    assert loader.cached(k) is None
    loader.request(k, images.url + '/ok')
    wait(lambda: loader.cached(k) is not None)
    assert loader._path(k).read_bytes().startswith(b'\x89PNG')


def test_cancelled_old_generation_never_emits_and_close_aborts_without_wait(images):
    loader = images.make()
    seen = []
    loader.ready.connect(lambda *args: seen.append(args))
    loader.request(key(images, '/slow-old'), images.url + '/slow-old')
    wait(lambda: images.calls['/slow-old'] == 1)
    old = loader._generation
    loader.begin()
    loader.request(key(images, '/ok'), images.url + '/ok')
    wait(lambda: bool(seen))
    assert all(x[0] != old for x in seen)
    loader.request(key(images, '/slow-close'), images.url + '/slow-close')
    wait(lambda: images.calls['/slow-close'] == 1)
    started = time.monotonic()
    loader.close()
    assert time.monotonic() - started < .1
    wait(lambda: not loader._active)
    assert not loader._queue


def test_panel_results_precede_posters_and_old_delivery_is_ignored(scene, images):
    from PySide6.QtCore import Qt
    panel = scene.panel
    rows = [{'vod_id': 'same', '_source': 'A', 'vod_name': 'Generated title',
             'vod_pic': images.url + '/slow-panel', 'first_url': 'synthetic'}]
    scene.mpv.reset_mock()
    panel._on_search_done(rows)
    assert panel.list_results.count() == panel.list_posters.count() == 1
    assert 'Generated title' in panel.list_results.item(0).text()
    item = panel.list_posters.item(0)
    k = item.data(Qt.UserRole + 82)
    assert not item.icon().isNull() and panel._search_posters.cached(k) is None
    wait(lambda: images.calls['/slow-panel'] == 1)
    generation = panel._search_posters._generation
    panel._on_search_done([dict(rows[0], _source='B', vod_pic=images.url + '/ok')])
    new_item = panel.list_posters.item(0)
    before = new_item.icon().cacheKey()
    fake = QImage(8, 8, QImage.Format_RGB32)
    fake.fill(QColor('red'))
    from PySide6.QtGui import QPixmap
    panel._on_search_poster_done(generation, new_item.data(Qt.UserRole + 82), QPixmap.fromImage(fake))
    assert new_item.icon().cacheKey() == before
    wait(lambda: panel._search_posters.cached(new_item.data(Qt.UserRole + 82)) is not None)
    scene.mpv.loadfile.assert_not_called()


@pytest.mark.parametrize('exit_kind', ['home', 'close'])
def test_panel_home_or_close_invalidates_active_pictures(scene, images, exit_kind):
    panel = scene.panel
    panel._on_search_done([{'vod_id': '1', '_source': 'A', 'vod_name': 'Generated',
                           'vod_pic': images.url + '/slow-panel', 'first_url': 'synthetic'}])
    wait(lambda: images.calls['/slow-panel'] == 1)
    if exit_kind == 'home':
        panel._show_movie_home()
    else:
        panel.close()
    wait(lambda: not panel._search_posters._active)
    assert not panel._search_posters._queue
    if exit_kind == 'close':
        assert panel._search_posters._closed


@pytest.mark.parametrize('path,success', [('/redirect', True), ('/loop', False)])
def test_cdn_redirects_have_a_finite_budget(images, path, success):
    loader = images.make()
    k = key(images, path)
    loader.request(k, images.url + path)
    wait(lambda: not loader._active)
    assert (loader.cached(k) is not None) is success
    assert images.calls[path] <= 3


def test_byte_cap_also_applies_without_content_length(images):
    loader = images.make()
    loader.MAX_BYTES = 1000
    k = key(images, '/no-length')
    loader.request(k, images.url + '/no-length')
    wait(lambda: not loader._active)
    assert loader.cached(k) is None and not loader._path(k).exists()


def test_all_legacy_cache_files_are_preserved(scene):
    from app.ui.panels import movie_panel
    directory = movie_panel._POSTER_DIR
    directory.mkdir(exist_ok=True)
    for i in range(542):
        (directory / f'{i}.jpg').write_bytes(b'legacy-cache-preserved')
    scene.panel._cleanup_poster_cache()
    assert len(list(directory.glob('*.jpg'))) == 542
    assert all(p.read_bytes() == b'legacy-cache-preserved' for p in directory.glob('*.jpg'))


def test_internal_decode_exception_releases_slot_and_reply(images, monkeypatch):
    loader = images.make()
    def broken(_):
        raise RuntimeError('synthetic codec error')
    monkeypatch.setattr(loader, '_decode', broken)
    k = key(images, '/ok')
    loader.request(k, images.url + '/ok')
    wait(lambda: not loader._active)
    assert not loader._pending and k in loader._failures
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not loader._manager.findChildren(QNetworkReply)


def test_detail_uses_qualified_cached_poster_not_unassigned_legacy(scene, images, monkeypatch):
    from app.ui.panels import movie_panel
    from app.ui.panels.movie_panel import _MovieDetailDialog
    panel = scene.panel
    row = dict(vod_id=1, _source='A', vod_name='Generated', vod_pic=images.url + '/ok')
    k = panel._search_poster_key(row)
    panel._search_posters.request(k, row['vod_pic'])
    wait(lambda: panel._search_posters.cached(k) is not None)
    # A same-id legacy file has no proven source ownership.
    monkeypatch.setattr(movie_panel, '_get_poster', lambda _: pytest.fail('unqualified CMS cache access'))
    dialog = _MovieDetailDialog(row, panel)
    color = dialog.lbl_pic.pixmap().toImage().pixelColor(0, 0)
    assert color == QColor('#369ad5')
    dialog.close()


def test_detail_does_not_use_wrong_source_legacy_when_new_cache_missing(scene, images, monkeypatch):
    from app.ui.panels import movie_panel
    from app.ui.panels.movie_panel import _MovieDetailDialog, _make_poster_placeholder
    row = dict(vod_id=1, _source='B', vod_name='Generated', vod_pic=images.url + '/not-requested')
    monkeypatch.setattr(movie_panel, '_get_poster', lambda _: pytest.fail('unqualified CMS cache access'))
    dialog = _MovieDetailDialog(row, scene.panel)
    from PySide6.QtCore import Qt
    actual = dialog.lbl_pic.pixmap()
    assert actual is not None and not actual.isNull()
    expected = _make_poster_placeholder(row['vod_name']).scaled(actual.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    assert dialog.lbl_pic.pixmap().toImage() == expected.toImage()
    assert not images.calls
    dialog.close()


def test_memory_lru_and_pending_queue_remain_bounded(images):
    loader = images.make()
    loader.MAX_MEMORY = 2
    from PySide6.QtGui import QPixmap
    pix = QPixmap(2, 2)
    pix.fill(QColor('blue'))
    keys = [key(images, f'/cached-{i}') for i in range(3)]
    for k in keys:
        loader._remember(k, pix)
    assert list(loader._memory) == keys[1:]
    assert loader.cached(keys[0]) is None
    assert loader.cached(keys[1]) is not None
    assert list(loader._memory) == [keys[2], keys[1]]
    for i in range(130):
        loader.request(key(images, f'/slow-{i}'), images.url + f'/slow-{i}')
    wait(lambda: sum(images.calls.values()) == 2)
    assert len(loader._active) == 2
    assert len(loader._queue) == loader.MAX_PENDING == 100
    assert len(loader._pending) == loader.MAX_PENDING + loader.MAX_ACTIVE
    loader.close()
    wait(lambda: not loader._active)
    assert not loader._pending and not loader._queue


def test_repeated_generation_cancellation_releases_replies_and_timers(images, record_property):
    import psutil
    process = psutil.Process()
    loader = images.make()
    # Warm up this local transport before recording process resources.
    loader.request(key(images, '/ok'), images.url + '/ok')
    wait(lambda: not loader._active)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    before = process.num_threads(), process.num_handles(), process.memory_info().rss
    for i in range(12):
        loader.request(key(images, f'/slow-cycle-{i}'), images.url + f'/slow-cycle-{i}')
        wait(lambda: images.calls[f'/slow-cycle-{i}'] == 1)
        loader.begin()
        wait(lambda: not loader._active)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert not loader._manager.findChildren(QNetworkReply)
        assert not loader._pending and not loader._queue
    # HTTP handler work is owned by the fixture too, even after peer abort.
    images.release.set()
    wait(lambda: images.active[0] == 0)
    record_property('handles_before_mock_server_thread_cleanup', process.num_handles())
    # do_GET returning is not the same as the mock server releasing its owned
    # Thread objects/Windows handles. Do not attribute those to the Qt client.
    images.drain_handlers()
    after = process.num_threads(), process.num_handles(), process.memory_info().rss
    record_property('resource_before_threads_handles_rss', before)
    record_property('resource_after_threads_handles_rss', after)
    assert after[0] <= before[0] + 2
    assert after[1] <= before[1] + 8
    assert len(loader._memory) <= loader.MAX_MEMORY
