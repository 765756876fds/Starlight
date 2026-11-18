"""Loopback only: a server that keeps sending bytes must not hold search forever."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from app.movie.movie_controller import MovieController


@pytest.fixture
def slow_cms():
    release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            self.server.paths.append(path)
            self.server.received.set()
            if path.endswith(("/ok", "/empty", "/invalid", "/large", "/error")):
                data = (b'{"list": [{"vod_id": 1, "vod_name": "Public test", "vod_play_url": "film$https://synthetic.invalid/media"}]}'
                        if path.endswith("/ok") else b"x" * 2_000_001 if path.endswith("/large")
                        else b"<html>not CMS</html>" if path.endswith("/invalid") else b'{"list": []}')
                self.send_response(503 if path.endswith("/error") else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (ConnectionError, OSError):
                    pass
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                # Socket remains active well inside timeout=.12, for up to 1s.
                end = time.monotonic() + 1
                while time.monotonic() < end and not release.is_set():
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    release.wait(.01)
                self.wfile.write(b'{"list": []}')
            except (ConnectionError, OSError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.paths = []
    server.received = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/api"
    release.set()
    server.shutdown()
    server.server_close()
    thread.join(2)
    assert not thread.is_alive()


def test_dripping_cms_response_obeys_total_search_read_budget(slow_cms):
    controller = MovieController.__new__(MovieController)
    started = time.monotonic()
    with pytest.raises(requests.Timeout):
        controller._search_one(slow_cms, "公开测试片名", timeout=.12)
    assert time.monotonic() - started < .65


@pytest.mark.parametrize("route,exception", [
    ("invalid", ValueError), ("large", ValueError), ("error", requests.HTTPError),
])
def test_http_or_payload_failure_is_not_reported_as_no_matches(slow_cms, route, exception):
    controller = MovieController.__new__(MovieController)
    with pytest.raises(exception):
        controller._search_one(slow_cms + "/" + route, "公开测试片名", timeout=1)


@pytest.mark.parametrize("route,count", [("ok", 1), ("empty", 0)])
def test_real_http_success_and_empty_response(slow_cms, route, count):
    controller = MovieController.__new__(MovieController)
    rows = controller._search_one(slow_cms + "/" + route, "公开测试片名", timeout=1)
    assert len(rows) == count


def search_controller(sources):
    from types import SimpleNamespace
    c = MovieController.__new__(MovieController)
    c._search_local = threading.local()
    c._source_manager = SimpleNamespace(get_enabled_sources=lambda: sources,
                                        get_active_name=lambda: sources[0]["name"])
    return c


def test_one_timed_out_source_can_fall_back_to_existing_second_source(slow_cms):
    c = search_controller([{"name": "slow", "url": slow_cms},
                           {"name": "good", "url": slow_cms + "/ok"}])
    rows = c.search("公开测试片名", timeout=.12)
    assert len(rows) == 1 and rows[0]["_source"] == "good"
    assert c.last_search_source == "good" and not c.last_search_error


def test_all_sources_fail_without_attempting_fourth(slow_cms):
    from unittest.mock import patch
    c = search_controller([{"name": str(i), "url": slow_cms + f"/{i}/error"} for i in range(4)])
    original = c._search_one
    with patch.object(c, "_search_one", wraps=original) as read:
        assert c.search("公开测试片名", timeout=.2) == []
        assert read.call_count == 3
    assert c.last_search_source is None and c.last_search_error.count("HTTPError") == 3


def test_expired_ui_context_stops_before_requesting_another_source(slow_cms):
    from unittest.mock import patch
    c = search_controller([{"name": "slow", "url": slow_cms},
                           {"name": "good", "url": slow_cms + "/ok"}])
    original = c._search_one
    with patch.object(c, "_search_one", wraps=original) as read:
        with pytest.raises(TimeoutError):
            c.search_for_ui("公开测试片名", lambda: True, time.monotonic() + .12)
        assert read.call_count == 1
    assert c._search_local.context is None
