"""Real CMS parsing with synthetic responses; only temporary source configuration."""
from io import BytesIO
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from urllib3.response import HTTPResponse

from app.movie import movie_controller as module
from app.movie.movie_controller import MovieController
from app.movie.source_manager import MovieSourceManager


@pytest.fixture
def search_case(tmp_path, monkeypatch):
    sources = [{"name": name, "url": f"https://cms-{i}.invalid/api", "enabled": True}
               for i, name in enumerate(("current", "other", "third", "极速资源"))]
    path = tmp_path / "settings.yaml"
    path.write_text(json.dumps({"movie": {"sources": sources, "active_source": "current"},
                                "unrelated": "preserve"}, ensure_ascii=False), encoding="utf8")
    manager = MovieSourceManager(str(path))
    manager._save = Mock(wraps=manager._save)
    controller = MovieController.__new__(MovieController)
    controller._source_manager = manager
    controller._search_local = threading.local()
    controller._manual_search_lock = threading.Lock()
    controller._manual_search_serial = 0
    controller._manual_search_preferred = None
    clock, calls, modes = [100.0], [], {}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    rows = [dict(vod_id=i, vod_name=f"Public {i}", vod_play_url=f"film$https://video.invalid/{i}.m3u8")
            for i in (1, 2)]

    def get(url, **kwargs):
        name = next((s["name"] for s in manager.get_all_sources() if s["url"] == url), "missing")
        calls.append((name, url, kwargs))
        mode = modes.get(name, "ok" if name == "极速资源" else "connect")
        clock[0] += 1.8 if mode in ("ok", "empty") else 4.0
        if callable(mode):
            mode = mode()
        if mode == "connect":
            raise requests.ConnectTimeout("synthetic connect failure")
        if mode == "read":
            raise requests.ReadTimeout("synthetic read failure")
        response = requests.Response()
        response.status_code = 503 if mode == "http" else 200
        response.encoding = "utf8"
        data = b"<html>not CMS</html>" if mode == "invalid" else json.dumps({"list": [] if mode == "empty" else rows}).encode()
        response.raw = HTTPResponse(body=BytesIO(data), preload_content=False)
        return response

    monkeypatch.setattr(module.requests, "get", get)

    def manual(current=lambda: True):
        return controller.search_for_ui("Public", current, clock[0] + 30)

    return SimpleNamespace(c=controller, m=manager, path=path, clock=clock, calls=calls,
                           modes=modes, rows=rows, manual=manual,
                           names=lambda: [call[0] for call in calls])


def test_first_then_repeat_uses_same_results_without_repeating_failed_connect(search_case, record_property):
    s = search_case
    original = s.path.read_bytes()
    started = s.clock[0]
    first = s.manual()
    assert s.names() == ["current", "极速资源"]
    first_cost = s.clock[0] - started
    s.calls.clear()
    started = s.clock[0]
    second = s.manual()
    repeat_cost = s.clock[0] - started
    assert s.names() == ["极速资源"]
    assert first_cost == pytest.approx(5.8) and repeat_cost == pytest.approx(1.8)
    assert first == second and len(first) == 2
    assert first[0]["_source"] == "极速资源"
    assert first[0]["play_url"] == s.rows[0]["vod_play_url"]
    assert first[0]["first_url"] == "https://video.invalid/1.m3u8"
    assert s.path.read_bytes() == original
    s.m._save.assert_not_called()
    assert s.m.get_active_name() == "current"
    assert s.calls[0][2]["timeout"] == (2.0, 10.0)
    assert s.calls[0][2]["params"] == {"ac": "videolist", "wd": "Public"}
    record_property("synthetic_first_seconds", first_cost)
    record_property("synthetic_repeat_seconds", repeat_cost)
    record_property("result_count_both", len(first))


@pytest.mark.parametrize("age,first_name", [(599, "极速资源"), (600, "current"), (601, "current")])
def test_ten_minute_expiry(search_case, age, first_name):
    s = search_case
    s.manual()
    s.clock[0] += age
    s.calls.clear()
    s.manual()
    assert s.names()[0] == first_name


@pytest.mark.parametrize("chosen", ["current", "other"])
def test_explicit_selection_including_same_name_invalidates(search_case, chosen):
    s = search_case
    s.manual()
    before = s.m.get_search_revision()
    s.m.set_active(chosen)
    assert s.m.get_search_revision() > before
    s.calls.clear()
    s.manual()
    assert s.names() == [chosen, "极速资源"]
    assert s.m.get_active_name() == chosen
    s.m._save.assert_called_once()
    assert "search_revision" not in s.path.read_text(encoding="utf8")


@pytest.mark.parametrize("change", ["disable", "remove", "address", "rename", "toggle_back"])
def test_source_changes_drop_old_cooldown(search_case, change):
    s = search_case
    s.manual()
    old_address = s.m.get_all_sources()[-1]["url"]
    if change == "disable":
        s.m.enable_source("极速资源", False)
    elif change == "remove":
        s.m.remove_source("极速资源")
    elif change == "toggle_back":
        s.m.enable_source("other", False)
        s.m.enable_source("other", True)
    else:
        data = json.loads(s.path.read_text(encoding="utf8"))
        if change == "address":
            data["movie"]["sources"][-1]["url"] = "https://new-cms.invalid/api"
        else:
            data["movie"]["sources"][-1]["name"] = "renamed"
        s.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
        s.m._load()
    s.calls.clear()
    s.manual()
    assert s.names()[0] == "current"
    if change in ("disable", "remove", "address", "rename"):
        assert old_address not in [call[1] for call in s.calls]


@pytest.mark.parametrize("mode", ["empty", "connect", "http", "invalid"])
def test_preferred_failure_keeps_three_source_fallback_and_forgets_bad_evidence(search_case, mode):
    s = search_case
    s.manual()
    s.modes["极速资源"] = mode
    s.modes["other"] = "ok"
    s.calls.clear()
    result = s.manual()
    assert s.names() == ["极速资源", "current", "other"]
    assert result[0]["_source"] == "other"
    s.calls.clear()
    s.manual()
    assert s.names()[0] == "current"


@pytest.mark.parametrize("mode", ["empty", "read", "http", "invalid", "ok"])
def test_no_learning_without_current_connect_timeout(search_case, mode):
    s = search_case
    s.modes["current"] = mode
    s.manual()
    s.calls.clear()
    s.manual()
    assert s.names()[0] == "current"


def test_legacy_search_does_not_use_or_refresh_manual_cooldown(search_case):
    s = search_case
    s.manual()
    original = s.c._manual_search_preferred
    s.calls.clear()
    s.c.search("Public")
    assert s.names() == ["current", "极速资源"]
    assert s.c._manual_search_preferred is original


def test_stale_reply_cannot_install_or_overwrite_newer_decision(search_case):
    s = search_case
    started, release = threading.Event(), threading.Event()
    errors = []

    def blocked():
        started.set()
        assert release.wait(2)
        return "ok"

    s.modes["极速资源"] = blocked
    def old_request():
        try:
            s.manual()
        except TimeoutError:
            errors.append("stale")
    worker = threading.Thread(target=old_request)
    worker.start()
    try:
        assert started.wait(1)
        s.modes["极速资源"] = "ok"
        s.manual()
        fresh = s.c._manual_search_preferred
        assert fresh is not None
        release.set()
        worker.join(2)
        assert not worker.is_alive() and errors == ["stale"]
        assert s.c._manual_search_preferred is fresh
        s.calls.clear()
        s.manual()
        assert s.names() == ["极速资源"]
    finally:
        release.set()
        worker.join(2)
        assert not worker.is_alive()


def test_cancelled_request_does_not_learn(search_case):
    s = search_case
    current = [True]
    def late():
        current[0] = False
        return "ok"
    s.modes["极速资源"] = late
    with pytest.raises(TimeoutError):
        s.manual(lambda: current[0])
    assert s.c._manual_search_preferred is None
    s.modes["极速资源"] = "ok"
    s.calls.clear()
    s.manual()
    assert s.names() == ["current", "极速资源"]


def test_same_name_selection_during_request_cannot_publish_old_evidence(search_case):
    s = search_case
    def changed():
        s.m.set_active("current")
        return "ok"
    s.modes["极速资源"] = changed
    s.manual()
    assert s.c._manual_search_preferred is None
    s.modes["极速资源"] = "ok"
    s.calls.clear()
    s.manual()
    assert s.names() == ["current", "极速资源"]


def test_expired_request_cannot_reorder_or_refresh(search_case):
    s = search_case
    s.manual()
    original = s.c._manual_search_preferred
    with pytest.raises(TimeoutError):
        s.c.search_for_ui("Public", lambda: True, s.clock[0])
    assert s.c._manual_search_preferred is original


def test_fresh_controller_cannot_inherit_process_local_observation(search_case):
    s = search_case
    s.manual()
    s.c._manual_search_preferred = None
    s.calls.clear()
    s.manual()
    assert s.names() == ["current", "极速资源"]


def test_successful_warm_search_does_not_extend_ten_minute_window(search_case):
    s = search_case
    s.manual()
    learned_at = s.clock[0]
    s.clock[0] = learned_at + 590
    s.calls.clear()
    s.manual()
    assert s.names() == ["极速资源"]
    s.clock[0] = learned_at + 600
    s.calls.clear()
    s.manual()
    assert s.names() == ["current", "极速资源"]


def test_late_failure_of_old_warm_request_cannot_clear_newer_observation(search_case):
    s = search_case
    s.manual()
    started, release = threading.Event(), threading.Event()
    errors = []

    def late_failure():
        started.set()
        assert release.wait(2)
        return "connect"

    s.modes["极速资源"] = late_failure
    def old_request():
        try:
            s.manual()
        except TimeoutError:
            errors.append("stale")
    worker = threading.Thread(target=old_request)
    worker.start()
    try:
        assert started.wait(1)
        s.modes["极速资源"] = "ok"
        s.manual()
        fresh = s.c._manual_search_preferred
        release.set()
        worker.join(2)
        assert not worker.is_alive() and errors == ["stale"]
        assert s.c._manual_search_preferred is fresh
        s.calls.clear()
        s.manual()
        assert s.names() == ["极速资源"]
    finally:
        release.set()
        worker.join(2)
        assert not worker.is_alive()
