"""No real endpoints/DB: slow connections must leave budget for source three."""
from io import BytesIO
import threading
from types import SimpleNamespace

import requests
from urllib3.response import HTTPResponse

from app.movie import movie_controller as module
from app.movie.movie_controller import MovieController


def connection_scenario(monkeypatch, clock):
    calls = []
    sources = [{"name": name, "url": f"https://{name}.invalid/cms"}
               for name in ("first", "second", "healthy")]
    controller = MovieController.__new__(MovieController)
    controller._search_local = threading.local()
    controller._source_manager = SimpleNamespace(get_enabled_sources=lambda: sources,
                                                 get_active_name=lambda: "first")
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def get(url, **kwargs):
        value = kwargs["timeout"]
        connect = value[0] if isinstance(value, tuple) else value
        calls.append((url, kwargs))
        if "healthy.invalid" not in url:
            # Controlled two-address latency model, not a claim about live DNS.
            clock[0] += 2 * connect
            raise requests.ConnectTimeout("synthetic connect failure")
        response = requests.Response()
        response.status_code = 200
        response.encoding = "utf-8"
        response.raw = HTTPResponse(body=BytesIO(
            b'{"list":[{"vod_id":1,"vod_name":"Public test","vod_play_url":"film$synthetic"}]}'),
            preload_content=False)
        return response
    monkeypatch.setattr(module.requests, "get", get)
    return controller, calls


def test_healthy_third_source_is_reached_within_existing_total_budget(monkeypatch):
    clock = [0.]
    controller, calls = connection_scenario(monkeypatch, clock)
    result = controller.search_for_ui("公开测试片名", lambda: True, 30.)
    assert len(result) == 1 and result[0]["_source"] == "healthy"
    assert len(calls) == 3 and clock[0] < 30
    assert controller.last_search_source == "healthy" and not controller.last_search_error


def test_fast_connect_limit_preserves_body_read_budget(monkeypatch):
    controller, calls = connection_scenario(monkeypatch, [0.])
    controller._search_one("https://healthy.invalid/cms", "公开测试片名", timeout=10.)
    assert calls[0][1]["timeout"] == (2., 10.)
    assert calls[0][1]["params"] == {"ac": "videolist", "wd": "公开测试片名"}


def test_small_existing_timeout_is_never_increased(monkeypatch):
    controller, calls = connection_scenario(monkeypatch, [0.])
    controller._search_one("https://healthy.invalid/cms", "公开测试片名", timeout=.12)
    assert calls[0][1]["timeout"] == (.12, .12)
