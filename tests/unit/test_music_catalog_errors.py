"""Preserve actual bridge failures through the directory aggregation path."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.music.online.manager import OnlineSourceManager
from app.music.online.sources.js_source import JSMusicSource


@pytest.mark.parametrize("scenario,expected", [("timeout", "timeout"),
                                              ("unavailable", "unavailable"),
                                              ("write_failure", "unavailable"),
                                              ("plugin", "plugin"),
                                              ("no_response", "unknown"),
                                              ("legacy_null", "unknown"),
                                              ("empty_success", None)])
def test_catalogue_preserves_the_bridge_failure_category(scenario, expected):
    source = JSMusicSource.__new__(JSMusicSource)
    source._lock = threading.Lock()
    source._pending = {}
    source._note_call_timeout = Mock()
    source.close = Mock()
    stream = Mock()
    source._proc = SimpleNamespace(stdin=stream)

    def respond():
        for request_id in list(source._pending):
            box = source._pending.pop(request_id)
            box["result"] = ({"ok": True, "result": {"groups": [], "errors": {}}}
                             if scenario == "empty_success" else
                             {"ok": False, "error": "plugin diagnostic"} if scenario == "plugin" else
                             {"ok": True, "result": None} if scenario == "legacy_null" else None)
            box["event"].set()

    if scenario == "unavailable":
        source._proc = None
    elif scenario == "write_failure":
        stream.write.side_effect = BrokenPipeError("isolated bridge pipe")
    elif scenario != "timeout":
        stream.flush.side_effect = respond
    # Real Event wait and _call implementation, with a bounded test deadline.
    source._call = lambda method, params, **_: JSMusicSource._call(source, method, params, timeout=.01)
    manager = OnlineSourceManager()
    manager.register(source)
    try:
        result = manager.get_top_lists()
        assert result == {}
        assert result.errors == ({"js_bridge": expected} if expected else {})
        assert source._pending == {}
        if scenario == "timeout":
            source._note_call_timeout.assert_called_once()
        else:
            source._note_call_timeout.assert_not_called()
    finally:
        manager.close()
