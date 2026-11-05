"""Real QNetworkAccessManager against a local generated-image server."""
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QEvent, QIODevice, QThread, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.ui.widgets.music_card_delegate import _CoverLoader
from app.ui.widgets.discovery_cards import CardList, DISPLAY_ROLE


def wait(predicate):
    for _ in range(150):
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "bounded Qt image wait expired"


@pytest.fixture
def scene(monkeypatch):
    app = QApplication.instance() or QApplication([])
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(QColor("#2377bb"))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    png = bytes(buffer.data())
    calls, active, released, release = Counter(), [], [], threading.Event()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            with lock:
                calls[self.path] += 1
                number = calls[self.path]
                active.append(self.path)
            try:
                if self.path.startswith("/slow"):
                    assert release.wait(3)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                    return
                bad = self.path == "/retry" and number == 1
                invalid = self.path == "/invalid"
                payload = b"not an image" if bad or invalid else png
                self.send_response(503 if bad else 200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    # Expected only when the owning loader deliberately closes.
                    pass
            finally:
                with lock:
                    released.append(self.path)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    loader = _CoverLoader()
    clock, done, threads = [0.], [], []
    loader._clock = lambda: clock[0]
    loader.loaded.connect(lambda key: (done.append(key), threads.append(QThread.currentThread())))
    monkeypatch.setattr("app.ui.widgets.discovery_cards._get_cover_loader", lambda: loader)
    s = SimpleNamespace(loader=loader, clock=clock, done=done, threads=threads, app=app,
                        calls=calls, active=active, released=released, release=release,
                        url=lambda path: f"http://127.0.0.1:{server.server_port}{path}")
    yield s
    release.set()
    close = getattr(loader, "close", None)
    if close:
        close()
    for reply in loader._qnam.findChildren(QNetworkReply):
        reply.abort()
    loader.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    server.shutdown()
    thread.join(1)
    server.server_close()
    assert not thread.is_alive()


def test_successful_image_is_decoded_cached_and_updated_on_gui_thread(scene):
    key = "net:" + scene.url("/ok")
    scene.loader.request(key)
    scene.loader.request(key)
    wait(lambda: len(scene.done) == 1)
    assert scene.loader.get(key).toImage().pixelColor(0, 0) == QColor("#2377bb")
    scene.loader.request(key)
    assert scene.calls["/ok"] == 1
    assert scene.threads == [scene.app.thread()]
    assert scene.loader._pending_count == 0


def test_failed_image_can_retry_after_cooldown_without_restarting_the_app(scene):
    key = "net:" + scene.url("/retry")
    scene.loader.request(key)
    wait(lambda: len(scene.done) == 1)
    assert scene.loader.failed(key) and scene.loader.get(key) is None
    scene.loader.request(key)
    assert scene.calls["/retry"] == 1
    scene.clock[0] = 31
    scene.loader.request(key)
    wait(lambda: len(scene.done) == 2)
    assert scene.calls["/retry"] == 2
    assert scene.loader.get(key) is not None and not scene.loader.failed(key)


def test_successful_cache_has_a_bounded_lifetime(scene):
    key = "net:" + scene.url("/ok")
    scene.loader.request(key)
    wait(lambda: len(scene.done) == 1)
    scene.clock[0] = 86401
    scene.loader.request(key)
    wait(lambda: len(scene.done) == 2)
    assert scene.calls["/ok"] == 2


def test_invalid_image_is_not_cached_as_a_success(scene):
    key = "net:" + scene.url("/invalid")
    scene.loader.request(key)
    wait(lambda: len(scene.done) == 1)
    assert scene.loader.failed(key) and scene.loader.get(key) is None
    assert scene.loader._pending_count == 0


def test_http_redirect_is_followed_and_decoded(scene):
    key = "net:" + scene.url("/redirect")
    scene.loader.request(key)
    wait(lambda: len(scene.done) == 1)
    assert scene.loader.get(key) is not None
    assert scene.calls["/redirect"] == scene.calls["/ok"] == 1


def test_active_requests_are_bounded_and_duplicate_requests_are_coalesced(scene):
    scene.loader.MAX_NETWORK_REQUESTS = 2
    for index in range(5):
        key = "net:" + scene.url(f"/slow{index}")
        scene.loader.request(key)
        scene.loader.request(key)
    wait(lambda: len(scene.active) >= 2)
    QTest.qWait(100)
    assert len(scene.active) == 2
    scene.release.set()
    wait(lambda: len(scene.done) == 5)
    assert len(set(scene.done)) == 5 and all(count == 1 for count in scene.calls.values())
    assert scene.loader._pending_count == 0


def test_cover_cache_cannot_grow_without_bound(scene):
    scene.loader.MAX_COVERS = 2
    for index in range(3):
        scene.loader.request("net:" + scene.url(f"/ok{index}"))
        wait(lambda: len(scene.done) == index + 1)
    assert len(scene.loader._covers) == 2


def test_decoded_cover_cache_keeps_thumbnails_instead_of_full_resolution_images(scene):
    image = QImage(1800, 1200, QImage.Format_RGB32)
    image.fill(QColor("#2377bb"))
    scene.loader._on_local_done("local:generated-only", image)
    pixmap = scene.loader.get("local:generated-only")
    assert pixmap is not None and max(pixmap.width(), pixmap.height()) <= 512
    assert pixmap.width() > pixmap.height()


def test_late_image_cannot_replace_the_current_card(scene):
    view = CardList(portrait=True)
    view.resize(350, 250)
    first, second = scene.url("/slow-old"), scene.url("/ok")
    describe = lambda row: dict(title=row["title"], cover_url=row["cover_url"])
    try:
        view.set_rows([dict(title="旧影片", cover_url=first)], describe)
        view.show()
        wait(lambda: scene.calls["/slow-old"] == 1)
        view.set_rows([dict(title="新影片", cover_url=second)], describe)
        wait(lambda: "net:" + second in scene.done)
        scene.release.set()
        wait(lambda: len(scene.done) == 2)
        assert view.item(0).data(DISPLAY_ROLE)["cover_url"] == second
        assert view.item(0).data(Qt.UserRole)["title"] == "新影片"
        assert not view.grab().isNull()
    finally:
        view.close()
        view.deleteLater()


def test_closing_loader_does_not_start_queued_requests(scene):
    scene.loader.MAX_NETWORK_REQUESTS = 1
    scene.loader.request("net:" + scene.url("/slow-first"))
    scene.loader.request("net:" + scene.url("/never-start"))
    wait(lambda: scene.calls["/slow-first"] == 1)
    scene.loader.close()
    scene.release.set()
    QTest.qWait(50)
    assert scene.calls["/never-start"] == 0 and not scene.done
    assert scene.loader._pending_count == 0
