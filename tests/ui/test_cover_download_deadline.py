"""A slowly streaming local image must not keep the shared cover queue busy."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.ui.widgets.music_card_delegate import _CoverLoader

pytestmark = pytest.mark.ui


def test_streaming_image_has_an_absolute_deadline_and_releases_its_slot(caplog, monkeypatch):
    app = QApplication.instance() or QApplication([])
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(QColor("#2377bb"))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    png = bytes(buffer.data())
    assert len(png) * .02 > .8
    # Scale only the total budget; use a real streaming HTTP reply and real Qt
    # timers. The normal policy remains 8s idle / 15s total for slow CDNs.
    monkeypatch.setattr(_CoverLoader, "NETWORK_DEADLINE_MS", 200, raising=False)
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            for byte in png:
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    break
                if release.wait(.02):
                    break

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    loader, completed = _CoverLoader(), []
    loader.loaded.connect(completed.append)
    key = f"net:http://127.0.0.1:{server.server_port}/generated.png"
    started = time.monotonic()
    try:
        loader.request(key)
        # Continuous bytes bypass an idle timeout, but not the total budget.
        while not completed and time.monotonic() - started < .7:
            QTest.qWait(20)
        assert completed == [key], "continuous bytes bypassed the total image download budget"
        assert loader.failed(key)
        assert loader.get(key) is None
        assert not loader._network_active and not loader._pending
        assert any("category=timeout_or_cancelled" in message for message in caplog.messages)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert not loader._qnam.findChildren(QNetworkReply)
    finally:
        release.set()
        loader.close()
        loader.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        server.shutdown()
        server.server_close()
        serving.join(1)
        assert not serving.is_alive()
