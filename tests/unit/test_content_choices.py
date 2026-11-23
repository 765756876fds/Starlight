"""Recording identity and bounded resolution fallback; no external sources."""
from dataclasses import replace
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from app.music.online.content_choices import group_tracks
from app.music.online.models import OnlineTrack
from tests.unit.test_music_loadfile_failure import FakeMpv, _make_ctrl


def song(source="A", **values):
    return replace(OnlineTrack(source, source, "星桥", "纸鸢乐队", "夜航", 180), **values)


@pytest.mark.parametrize("change", [{"artist":"另一位"}, {"title":"星桥 Live"},
    {"album":"另一张"}, {"duration_seconds":181}, {"album":""},
    {"artist":"未知"}, {"duration_seconds":0}, {"duration_seconds":float("nan")},
    {"extra":{"_raw":{"version":"翻唱"}}}])
def test_different_or_incomplete_recordings_stay_separate(change):
    assert len(group_tracks([song(), song("B", **change)])) == 2


def test_equivalent_recordings_preserve_every_payload_and_first_result_order():
    a, b, c = song(), song("B"), song("C", title="别的歌")
    groups = group_tracks([a, c, b])
    assert groups == [[a, b], [c]]
    assert groups[0][1] is b


@pytest.mark.parametrize("first", [None, RuntimeError("fixture timeout")])
def test_missing_address_tries_same_recording_before_loading_once(tmp_path, first):
    fake = FakeMpv(); ctrl = _make_ctrl(tmp_path, fake)
    ctrl.online_manager = Mock()
    ctrl.online_manager.get_play_url.side_effect = [first, {"url":"https://media.invalid/same"}]
    try:
        assert ctrl.play_online(song(), _alternatives=[song("B"), song("C", title="翻唱")])
        assert [call.args[0].source_name for call in ctrl.online_manager.get_play_url.call_args_list] == ["A", "B"]
        assert [call for call in fake.calls if call[0] == "loadfile"] == [("loadfile", "https://media.invalid/same")]
    finally:
        ctrl.db.close()


@pytest.mark.parametrize("reply", [None, {"error":"failed"}])
def test_load_unknown_or_rejected_never_triggers_second_version(tmp_path, reply):
    fake = FakeMpv(loadfile_result=reply); ctrl = _make_ctrl(tmp_path, fake)
    ctrl.online_manager = Mock();ctrl.online_manager.get_play_url.return_value = {"url":"https://media.invalid/one"}
    try:
        assert not ctrl.play_online(song(), _alternatives=[song("B")])
        assert ctrl.online_manager.get_play_url.call_count == 1
        assert len([c for c in fake.calls if c[0] == "loadfile"]) == 1
    finally:
        ctrl.db.close()


def test_stale_resolution_does_not_attempt_alternative_or_load(tmp_path):
    fake = FakeMpv(); ctrl = _make_ctrl(tmp_path, fake)
    entered, release = Event(), Event()
    def slow(track):
        entered.set(); assert release.wait(2); return None
    ctrl.online_manager = Mock();ctrl.online_manager.get_play_url.side_effect = slow
    task = Thread(target=lambda: ctrl.play_online(song(), _alternatives=[song("B")]))
    try:
        task.start(); assert entered.wait(1)
        with ctrl._lock:
            ctrl._play_request_id += 1
        release.set();task.join(2)
        assert not task.is_alive()
        assert ctrl.online_manager.get_play_url.call_count == 1
        assert not [c for c in fake.calls if c[0] == "loadfile"]
    finally:
        release.set();task.join(2);ctrl.db.close()


def test_cancelled_resolution_clears_switching_without_loading(tmp_path):
    fake=FakeMpv();ctrl=_make_ctrl(tmp_path,fake)
    ctrl.online_manager=Mock();ctrl.online_manager.get_play_url.return_value=None
    try:
        assert not ctrl.play_online(song(),_alternatives=[song("B")],_before_load=lambda:False)
        assert not ctrl._is_switching_track
        assert ctrl.online_manager.get_play_url.call_count==1
        assert not [c for c in fake.calls if c[0]=="loadfile"]
    finally:
        ctrl.db.close()
