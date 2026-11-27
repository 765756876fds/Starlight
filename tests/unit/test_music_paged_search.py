"""Pagination, independent delivery, and actual outstanding request accounting."""
import json
import os
import threading
import time
from pathlib import Path

from app.music.online.manager import OnlineSourceManager
from app.music.online.models import OnlineTrack
from tests.music_search_fixture import paged_bridge  # noqa: F401


def pump(session, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        session.poll()
        if condition():
            return
        assert time.monotonic() < deadline, session.errors()
        time.sleep(0.004)


def test_bridge_independent_page_metadata_identity_and_retry(paged_bridge):
    manager = OnlineSourceManager()
    manager.register(paged_bridge)
    try:
        assert {p["name"]: p["capabilities"]["search"] for p in paged_bridge.list_plugins()} == {
            "fast": True, "slow": True, "lookup": False,
        }
        process, _ = paged_bridge.search_snapshot()
        direct = paged_bridge.search_page(process, "fast", "direct", 2)
        assert [track.extra["_raw"]["observedPage"] for track in direct.tracks] == [2, 2]
        assert direct.is_end is False and direct.total == 36

        session = manager.open_search("failure")
        session.request()
        pump(session, lambda: not session.loading())
        assert session.errors() == {("js_bridge", "fast"): "plugin", ("js_bridge", "slow"): "plugin"}
        assert session.results()["js_bridge"] == []
        session.request(retry=True)
        pump(session, lambda: not session.loading())
        valid = session.results()["js_bridge"]
        assert len(valid) == 34 and {track.extra["_raw"]["observedPage"] for track in valid} == {1}
        assert session.errors() == {("js_bridge", "slow"): "plugin"}

        session = manager.open_search("pages")
        for expected in (35, 36, 37):
            session.request()
            pump(session, lambda: not session.loading())
            assert len(session.results()["js_bridge"]) == expected
        songs = session.results()["js_bridge"]
        assert [track.extra["_raw"]["observedPage"] for track in songs if track.source_name == "fast"] == (
            [1] * 34 + [2, 3])
        assert {track.source_name for track in songs if track.track_id == "1"} == {"fast", "slow"}
        assert not session.can_fetch()
        session.request()
        assert not session.loading()
        assert len(session.results()["js_bridge"]) == 37
    finally:
        manager.close()


def test_bridge_timeout_retains_fast_results_and_real_active_count(paged_bridge):
    manager = OnlineSourceManager()
    manager.register(paged_bridge)
    try:
        session = manager.open_search("split")
        start = time.monotonic()
        session.request()
        pump(session, lambda: bool(session.results().get("js_bridge")))
        first = time.monotonic() - start
        assert session.loading() and not session.errors()
        assert len(session.results()["js_bridge"]) == 64
        stats = paged_bridge._call("searchStats", {})
        assert stats == {"active": 1, "peak": 2, "limit": 12}
        pump(session, lambda: not session.loading(), timeout=9)
        finish = time.monotonic() - start
        assert session.errors() == {("js_bridge", "slow"): "timeout"}
        assert len(session.results()["js_bridge"]) == 64

        for index in range(20):
            next_search = manager.open_search(f"next-{index}")
            next_search.request()
            pump(next_search, lambda: not next_search.loading())
            assert next_search.errors() == {("js_bridge", "slow"): "busy"}
            assert len(next_search.results()["js_bridge"]) == 34
            assert paged_bridge._call("searchStats", {}) == stats
        assert manager.paged_search_activity() == (0, 2)
        evidence = os.getenv("STARLIGHT_SEARCH_EVIDENCE")
        if evidence:
            Path(evidence).write_text(json.dumps({
                "first_fast_ms": round(first * 1000, 2),
                "slow_timeout_and_final_ms": round(finish * 1000, 2),
                "retained_fast_count": 64, "bridge_activity_after_20_searches": stats,
            }, indent=2), encoding="utf8")
    finally:
        manager.close()


def test_bridge_reload_invalidates_old_search_even_with_same_plugin_name(paged_bridge):
    manager = OnlineSourceManager()
    manager.register(paged_bridge)
    try:
        old = manager.open_search("reload")
        old.request()
        old.poll()
        assert paged_bridge.reload()
        assert old.poll()
        assert not old.results()["js_bridge"]
        assert set(old.errors().values()) == {"interrupted"}
        new = manager.open_search("new")
        new.request()
        pump(new, lambda: not new.loading())
        assert len(new.results()["js_bridge"]) == 35
    finally:
        manager.close()


def test_legacy_adapter_is_single_page_and_replacement_keeps_real_ledger():
    class Source:
        name = "native"

        def __init__(self, blocked=False):
            self.calls = []
            self.closed = False
            self.release = threading.Event()
            if not blocked:
                self.release.set()

        def search(self, keyword, limit):
            self.calls.append(keyword)
            assert self.release.wait(5)
            return [OnlineTrack(self.name, "x", keyword)]

        def close(self):
            self.closed = True

    manager = OnlineSourceManager()
    old_source, new_source = Source(blocked=True), Source()
    manager.register(old_source)
    try:
        old = manager.open_search("old")
        old.request()
        pump(old, lambda: bool(old_source.calls))
        manager.register(new_source)
        assert old_source.closed
        assert old.poll() and not old.results()["native"]
        assert manager.paged_search_activity() == (1, 1)
        fresh = manager.open_search("fresh")
        fresh.request()
        fresh.poll()
        assert fresh.loading() and not new_source.calls
        old_source.release.set()
        pump(fresh, lambda: not fresh.loading())
        assert [track.title for track in fresh.results()["native"]] == ["fresh"]
        assert not fresh.can_fetch()
        fresh.request()
        assert new_source.calls == ["fresh"]
        manager.unregister("native")
        assert fresh.poll() and not fresh.results()["native"]
        assert new_source.closed
    finally:
        old_source.release.set()
        manager.close()
