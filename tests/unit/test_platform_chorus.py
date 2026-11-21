"""Synthetic Kuwo metadata and playback identity. No plugin, audio service or database."""
import threading
from unittest.mock import Mock

import pytest

from app.music.controller import MusicController
from app.music.online.chorus import KuwoChorus
from app.music.online.models import OnlineTrack


def track():
    rid = "5886682"
    raw = {"id": rid, "refrain": {"rid": rid, "start_ms": "69358", "end_ms": "94837"}}
    return OnlineTrack("酷我音乐", rid, "海阔天空", duration_seconds=324, extra={"_raw": raw})


@pytest.mark.parametrize("start,end", [
    (None, 90000), (False, 90000), ("NaN", 90000), ("-1", 90000),
    (90000, 90000), (90000, 40000), (90000, 325000), (69.358, 94.837),
])
def test_bad_bounds(start, end):
    t = track()
    t.extra["_raw"]["refrain"].update(start_ms=start, end_ms=end)
    assert KuwoChorus.from_track(t, t.track_id, 7) is None


def test_id_and_resolution_must_match():
    t = track()
    assert KuwoChorus.from_track(t, None, 7) is None
    assert KuwoChorus.from_track(t, "326890", 7) is None
    t.extra["_raw"]["refrain"]["rid"] = "326890"
    assert KuwoChorus.from_track(t, t.track_id, 7) is None
    t = track()
    t.source_name = "网易音乐"
    assert KuwoChorus.from_track(t, t.track_id, 7) is None


@pytest.fixture
def playback():
    t = track()
    ctrl = MusicController.__new__(MusicController)
    ctrl._platform_chorus = KuwoChorus.from_track(t, t.track_id, 7)
    ctrl._running = True
    ctrl._user_stopped = ctrl._is_switching_track = False
    ctrl._play_request_id = ctrl._last_loadfile_epoch = 7
    ctrl._current_song = {"source_name": t.source_name, "track_id": t.track_id, "path": "sample"}
    ctrl._lock = threading.Lock()
    ctrl._seek_current = Mock()
    props = {"path": "sample", "duration": 324.1, "time-pos": 2.1}
    ctrl.mpv = Mock()
    ctrl.mpv.get_property.side_effect = lambda key, timeout: props[key]
    return ctrl, props


@pytest.mark.parametrize("prop,value", [
    ("path", None), ("path", "other"), ("duration", 30.0), ("duration", None),
    ("duration", float("nan")), ("time-pos", None), ("time-pos", 0),
])
def test_unconfirmed_media_never_seeks(playback, prop, value):
    ctrl, props = playback
    props[prop] = value
    assert ctrl.observe_chorus() is None
    ctrl.seek_chorus(ctrl._platform_chorus, threading.Event())
    ctrl._seek_current.assert_not_called()


def test_only_click_seeks_and_requires_current_epoch(playback):
    ctrl, _ = playback
    marker, duration = ctrl.observe_chorus()
    assert (marker.start, marker.end, duration) == (69.358, 94.837, 324.1)
    ctrl._seek_current.assert_not_called()
    cancel = threading.Event()
    ctrl.seek_chorus(marker, cancel)
    ctrl._seek_current.assert_called_once_with(69.358, "absolute")
    ctrl._seek_current.reset_mock()
    cancel.set()
    ctrl.seek_chorus(marker, cancel)
    ctrl._play_request_id += 1
    ctrl.seek_chorus(marker, threading.Event())
    assert ctrl.observe_chorus() is None
    ctrl._seek_current.assert_not_called()
