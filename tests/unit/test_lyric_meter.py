"""MPV audio meter and playback-clock contracts, independent of audio hardware."""
import time
import threading
from unittest.mock import Mock

import pytest

from app.music.controller import MusicController
from app.music.mpv import MpvController


def meter_with(replies):
    mpv = MpvController("mpv")
    mpv._pipe = object()
    mpv._send_command = Mock(side_effect=replies)
    return mpv


def metadata(rms, peak):
    return {"data": {"lavfi.astats.Overall.RMS_level": rms, "lavfi.astats.Overall.Peak_level": peak}}


def test_measure_once_reuse_and_remove_only_our_labeled_filter():
    mpv = meter_with([{"error": "success"}, metadata("-28.5", "-5"), metadata("-inf", "-inf"), {"error": "success"}])
    assert mpv.lyric_levels(True) == pytest.approx((.5, 1.))
    assert mpv.lyric_levels(True) == (0., 0.)
    assert mpv.lyric_levels(False) is None
    assert mpv.lyric_levels(False) is None
    calls = [c.args for c in mpv._send_command.call_args_list]
    assert calls[0][0] == "af" and calls[0][1][0] == "add"
    assert calls[0][1][1].startswith("@starlight_lyric_level:lavfi=[astats=")
    assert calls[1:3] == [("get_property", ["af-metadata/starlight_lyric_level"])] * 2
    assert calls[-1] == ("af", ["remove", "@starlight_lyric_level"])


@pytest.mark.parametrize("bad", [None, {}, {"data": "oops"}, metadata("nan", "-5"), metadata("-32", "inf")])
def test_unavailable_metadata_does_not_invent_silence_or_energy(bad):
    mpv = meter_with([{"error": "success"}, bad])
    assert mpv.lyric_levels(True) is None


def test_failed_or_interrupted_filter_is_not_reinstalled_every_poll():
    rejected = meter_with([{"error": "command failed"}])
    for _ in range(3):
        assert rejected.lyric_levels(True) is None
    rejected.lyric_levels(False)
    assert rejected._send_command.call_count == 1
    unknown = meter_with([None, {"error": "success"}])
    assert unknown.lyric_levels(True) is None
    assert unknown.lyric_levels(True) is None
    unknown.lyric_levels(False)
    assert unknown._send_command.call_args.args == ("af", ["remove", "@starlight_lyric_level"])


def test_audio_transport_has_priority_and_a_closed_or_movie_pipe_is_unused():
    mpv = meter_with([])
    with mpv._pipe_lock:
        assert mpv.lyric_levels(True) is None
    mpv.video = True
    assert mpv.lyric_levels(True) is None
    mpv.video = False
    mpv._pipe = None
    assert mpv.lyric_levels(True) is None
    mpv._send_command.assert_not_called()


def test_confirmed_speed_reanchors_lyrics_without_a_jump_or_an_ipc_read(monkeypatch):
    ctrl = MusicController.__new__(MusicController)
    ctrl._mpv_available = True
    ctrl.mpv = Mock()
    ctrl.mpv.set_property.return_value = {"error": "success"}
    ctrl.state_store = Mock()
    ctrl._lock = threading.Lock()
    ctrl._speed = 1.
    ctrl._playback_time_cache = (0., 0., False)
    now = [105.]
    monkeypatch.setattr("app.music.controller.time.perf_counter", lambda: now[0])
    assert ctrl.current_playback_time == 0.
    ctrl._playback_time_cache = (15., 100., False)
    assert ctrl.current_playback_time == 20.
    assert ctrl.set_speed(1.5) is True
    assert ctrl.current_playback_time == 20.
    now[0] += 2.
    assert ctrl.current_playback_time == 23.
    ctrl._playback_time_cache = (23., now[0], True)
    now[0] += 2.
    assert ctrl.current_playback_time == 23.
    ctrl.mpv.get_property.assert_not_called()


@pytest.mark.parametrize("replaced", [False, True])
def test_eof_refresh_checks_the_real_transport_and_rejects_a_replaced_clock(monkeypatch, replaced):
    ctrl = MusicController.__new__(MusicController)
    song = {"path": "B.wav"}
    ctrl._running = True
    ctrl._current_song = song
    ctrl._play_request_id = 8
    ctrl._lock = threading.Lock()
    ctrl._paused_snapshot = False
    ctrl._playback_time_cache = (0., 0., False)
    ctrl._schedule_skip_bad = Mock()
    ctrl.mpv = Mock()
    ctrl.mpv.is_idle.return_value = True
    now = [100.]
    reads = []
    monkeypatch.setattr("app.music.controller.time.monotonic", lambda: now[0])
    monkeypatch.setattr("app.music.controller.time.perf_counter", lambda: now[0])
    monkeypatch.setattr("app.music.controller.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))

    def read(prop, timeout):
        reads.append((prop, round(now[0] - 100., 3)))
        if prop == "idle-active":
            return False
        if prop == "playback-time" and replaced:
            ctrl._play_request_id += 1
            ctrl._playback_time_cache = (8., now[0], True)
        return .26 if prop == "playback-time" else 96.

    ctrl.mpv.get_property.side_effect = read
    ctrl._confirm_playback(song, "B.wav", 8)
    assert reads[:2] == [("idle-active", .045), ("playback-time", .045)]
    if replaced:
        assert len(reads) == 2 and ctrl._playback_time_cache[0] == 8.
    else:
        assert reads[-1] == ("duration", .8)
        assert ctrl._playback_time_cache[0] == .26
    ctrl._schedule_skip_bad.assert_not_called()


def test_confirmed_pause_and_resume_do_not_wait_for_another_position_poll(monkeypatch):
    ctrl = MusicController.__new__(MusicController)
    ctrl._speed = 1.5
    ctrl._playback_time_cache = (3., 100., False)
    now = [102.]
    monkeypatch.setattr("app.music.controller.time.perf_counter", lambda: now[0])
    ctrl._anchor_pause(True)
    now[0] = 108.
    assert ctrl.current_playback_time == 6. and ctrl._paused_snapshot
    ctrl._anchor_pause(False)
    now[0] += .2
    assert ctrl.current_playback_time == pytest.approx(6.3)
    anchor = ctrl._playback_time_cache
    ctrl._anchor_pause(False)
    assert ctrl._playback_time_cache is anchor


@pytest.mark.parametrize("response", [None, {"error": "failed"}, {"error": "success"}])
def test_pause_clock_uses_a_real_command_acknowledgement(response):
    from app.music.mpv import MpvController
    ctrl = MusicController.__new__(MusicController)
    ctrl._paused_snapshot = False
    ctrl._playback_time_cache = (1., time.perf_counter(), False)
    ctrl.mpv = MpvController.__new__(MpvController)
    ctrl.mpv._properties = {"pause": False}
    ctrl.mpv.set_property = Mock(return_value=response)
    ok = response == {"error": "success"}
    assert ctrl._request_pause(True) is ok
    assert ctrl._paused_snapshot is ok and ctrl._playback_time_cache[2] is ok
    assert ctrl.mpv.is_paused() is ok
