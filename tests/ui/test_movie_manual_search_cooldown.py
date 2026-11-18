"""Real manual UI/legacy voice routes, fake CMS and MPV, temporary settings only."""
from io import BytesIO
import json
import threading
from types import MethodType
from unittest.mock import Mock

import pytest
import requests
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from urllib3.response import HTTPResponse

from app.core.command_bus import Command, CommandType
from app.movie import movie_controller as module
from app.movie.movie_controller import MovieController
from app.movie.source_manager import MovieSourceManager
from app.ui.dialogs.movie_source_dialog import MovieSourceDialog
from tests.ui.test_movie_voice_window import scene as scene
from tests.ui.test_product_discovery import wait

pytestmark = pytest.mark.ui


@pytest.fixture
def manual_scene(scene, tmp_path, monkeypatch):
    s = scene
    sources = [dict(name=name, url=f"https://cms-{i}.invalid/api", enabled=True)
               for i, name in enumerate(("current", "other", "极速资源"))]
    path = tmp_path / "sources.yaml"
    path.write_text(json.dumps({"movie": {"sources": sources, "active_source": "current"}}), encoding="utf8")
    s.controller._source_manager = MovieSourceManager(str(path))
    s.controller._source_manager._save = Mock(wraps=s.controller._source_manager._save)
    s.controller.film_memory = Mock()
    monkeypatch.setattr(s.controller, "search", MethodType(MovieController.search, s.controller))
    s.cms_calls = []
    s.block_old = [False]
    s.started, s.release = threading.Event(), threading.Event()
    rows = [dict(vod_id=i, vod_name=f"Public {i}", vod_play_url=f"film$https://video.invalid/film-{i}.m3u8")
            for i in (1, 2)]

    def get(url, **kwargs):
        name = next(row["name"] for row in sources if row["url"] == url)
        query = kwargs["params"]["wd"]
        s.cms_calls.append((query, name))
        if name != "极速资源":
            raise requests.ConnectTimeout("synthetic")
        if query == "old" and s.block_old[0]:
            s.started.set()
            assert s.release.wait(2)
        response = requests.Response()
        response.status_code = 200
        response.encoding = "utf8"
        response.raw = HTTPResponse(body=BytesIO(json.dumps({"list": rows}).encode()), preload_content=False)
        return response

    monkeypatch.setattr(module.requests, "get", get)
    yield s, path
    s.release.set()
    wait(lambda: not s.panel._film_search._running)


def search(s, query):
    s.panel.edit_kw.setText(query)
    s.panel.btn_search.click()
    wait(lambda: not s.panel._film_search._running)
    assert s.panel.list_results.count() == s.panel.list_posters.count() == 2
    assert "正在搜索" not in s.panel.lbl_status.text()


def test_real_manual_entry_same_name_selection_and_voice_keep_payload(manual_scene):
    s, path = manual_scene
    original = path.read_bytes()
    search(s, "first")
    search(s, "repeat")
    assert s.cms_calls == [("first", "current"), ("first", "极速资源"), ("repeat", "极速资源")]
    s.mpv.loadfile.assert_not_called()
    assert path.read_bytes() == original
    manager = s.controller._source_manager
    manager._save.assert_not_called()
    # The actual settings dialog issues a same-name set_active, not a test-only flag.
    dialog = MovieSourceDialog(s.panel, manager=manager)
    dialog.list_widget.setCurrentRow(0)
    dialog.btn_set_active.click()
    dialog.close()
    search(s, "reselected")
    assert s.cms_calls[-2:] == [("reselected", "current"), ("reselected", "极速资源")]
    assert manager.get_active_name() == "current"
    manager._save.assert_called_once()
    # Existing label reflects configured default, not actual row origin; report only.
    assert "极速资源" in s.panel.lbl_status.text()
    assert s.panel._current_source_name() == "current"
    item = s.panel.list_posters.item(1)
    payload = item.data(Qt.UserRole)
    assert payload["vod_id"] == 2 and payload["_source"] == "极速资源"
    s.panel._on_item_activated(item)
    wait(lambda: s.mpv.loadfile.called)
    s.mpv.loadfile.assert_called_once_with("https://video.invalid/film-2.m3u8")
    assert s.controller.current_item()["_source"] == "极速资源"
    # Existing voice command bypasses only the manual cooldown, not the original player.
    s.cms_calls.clear()
    assert s.bus.execute(Command(CommandType.MOVIE_SEARCH_PLAY, {"query": "voice"}, source="voice")) is True
    assert s.cms_calls == [("voice", "current"), ("voice", "极速资源")]


def test_new_keyword_invalidates_old_observation_before_next_request(manual_scene):
    s, _ = manual_scene
    s.block_old[0] = True
    try:
        s.panel.edit_kw.setText("old")
        s.panel._do_search()
        assert s.started.wait(1)
        s.panel.edit_kw.setText("new")
        s.panel._do_search()
        s.release.set()
        wait(lambda: not s.panel._film_search._running and s.panel.list_posters.count() == 2)
        # The late old request cannot cause the new request to skip current.
        assert s.cms_calls == [("old", "current"), ("old", "极速资源"),
                               ("new", "current"), ("new", "极速资源")]
        search(s, "third")
        assert s.cms_calls[-1] == ("third", "极速资源")
        s.mpv.loadfile.assert_not_called()
    finally:
        s.release.set()
        QTest.qWait(10)
