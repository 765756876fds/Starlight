"""Local subtitle controls use real IPC contracts, not truthy mocks."""
import copy
import threading
from unittest.mock import Mock

import pytest

from app.movie.movie_controller import MovieController
from app.music.mpv import MpvController


class SubtitleMpv:
    def __init__(self, media):
        self.props = {"path": str(media), "sub-visibility": True, "sid": False,
                      "track-list": [{"type": "video", "id": 1}]}
        self.adds = []
        self.commands = []
        self.response = {"error": "success"}
        self.invalid = False
        self.before_add = None

    def is_running(self):
        return True

    def get_property(self, name, timeout=2.):
        return copy.deepcopy(self.props.get(name))

    def get_track_list(self):
        return self.get_property("track-list")

    def set_property(self, name, value):
        self.commands.append((name, value))
        self.props[name] = value
        return {"error": "success"}

    def set_sid(self, sid):
        return self.set_property("sid", sid)

    def sub_add(self, path):
        self.adds.append(path)
        if self.before_add:
            self.before_add(path)
        if self.response != {"error": "success"} or self.invalid:
            return self.response
        # mpv's cached flag reuses a track even if auto discovery won the race.
        existing = next((t for t in self.props["track-list"]
                         if t.get("external-filename") == path), None)
        if not existing:
            self.add_track(path)
        return self.response

    def add_track(self, path, lang=""):
        sid = len(self.props["track-list"])
        track = {"type": "sub", "id": sid, "external": True,
                 "external-filename": str(path), "lang": lang}
        self.props["track-list"].append(track)
        return sid


@pytest.fixture
def movie(tmp_path):
    video = tmp_path / "中文 电影.mp4"
    video.write_bytes(b"test video")
    sub = tmp_path / "中文 电影.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    ctl = MovieController.__new__(MovieController)
    ctl.mpv = SubtitleMpv(video)
    ctl._current = {"local_file": True, "first_url": str(video)}
    ctl._playback_gen = 1
    ctl._subtitle_lock = threading.RLock()
    ctl._pending_local_subtitle = None
    return ctl, video, sub


def test_mpv_sub_add_preserves_response_and_uses_native_cached_flag():
    mpv = MpvController.__new__(MpvController)
    response = {"error": "success"}
    mpv._send_command = Mock(return_value=response)
    assert mpv.sub_add("中文 电影.srt", "中文") is response
    mpv._send_command.assert_called_once_with(
        "sub-add", ["中文 电影.srt", "cached", "中文"], timeout=5.0)


def test_auto_same_file_is_not_added_twice_and_languages_are_kept(movie):
    ctl, video, sub = movie
    ctl.mpv.add_track(sub, "zh")
    english = sub.with_name("中文 电影.en.srt")
    ctl.mpv.add_track(english, "en")
    ctl._load_local_subtitle(str(video))
    assert ctl.mpv.adds == [], "自动发现的同一字幕不得再追加"
    ctl._apply_pending_local_subtitle()
    assert ctl.mpv.adds == []
    assert len([t for t in ctl.mpv.get_track_list() if t["type"] == "sub"]) == 2


def test_auto_waits_for_current_media_then_adds_once(movie):
    ctl, video, sub = movie
    ctl.mpv.props["path"] = "old.mp4"
    ctl._load_local_subtitle(str(video))
    ctl._apply_pending_local_subtitle()
    assert ctl.mpv.adds == []
    ctl.mpv.props["path"] = str(video)
    ctl._apply_pending_local_subtitle()
    ctl._apply_pending_local_subtitle()
    assert ctl.mpv.adds == [str(sub)]


def test_auto_fallback_does_not_reenable_hidden_subtitles(movie):
    ctl, video, _ = movie
    ctl.mpv.props["sub-visibility"] = False
    ctl._load_local_subtitle(str(video))
    ctl._apply_pending_local_subtitle()
    assert len(ctl.mpv.adds) == 1
    assert ctl.mpv.props["sub-visibility"] is False


def test_manual_add_selects_and_reuses_existing_file(movie):
    ctl, _, sub = movie
    assert ctl.load_local_subtitle(str(sub))[0] is True
    sid = ctl.mpv.props["sid"]
    ctl.mpv.props["sub-visibility"] = False
    assert ctl.load_local_subtitle(str(sub.parent / "." / sub.name))[0] is True
    assert ctl.mpv.adds == [str(sub)]
    assert ctl.mpv.props["sid"] == sid
    assert ctl.mpv.props["sub-visibility"] is True


def test_native_discovery_between_snapshot_and_add_is_deduplicated(movie):
    ctl, _, sub = movie
    ctl.mpv.before_add = lambda path: ctl.mpv.add_track(path, "zh")
    assert ctl.load_local_subtitle(str(sub))[0] is True
    assert len([t for t in ctl.mpv.get_track_list() if t["type"] == "sub"]) == 1


@pytest.mark.parametrize("response", [{"error": "error running command"}, None])
def test_add_error_or_missing_response_does_not_report_success(movie, response):
    ctl, _, sub = movie
    ctl.mpv.response = response
    ok, message = ctl.load_local_subtitle(str(sub))
    assert ok is False and message
    assert ctl.mpv.commands == []


def test_success_response_without_actual_track_is_not_success(movie):
    ctl, _, sub = movie
    ctl.mpv.invalid = True
    assert ctl.load_local_subtitle(str(sub))[0] is False
    assert ctl.mpv.commands == []


def test_missing_file_and_directory_do_not_send_commands(movie):
    ctl, _, sub = movie
    assert ctl.load_local_subtitle(str(sub.with_name("missing.srt")))[0] is False
    assert ctl.load_local_subtitle(str(sub.parent))[0] is False
    assert ctl.mpv.adds == []


def test_hide_reenable_and_select_specific_language(movie):
    ctl, video, sub = movie
    zh = ctl.mpv.add_track(sub, "zh")
    en = ctl.mpv.add_track(sub.with_name("英文.srt"), "en")
    assert ctl.select_subtitle(en, expected_media=str(video))[0] is True
    assert ctl.set_subtitle_visible(False, expected_media=str(video))[0] is True
    assert ctl.mpv.props["sid"] == en
    assert ctl.set_subtitle_visible(True, expected_media=str(video))[0] is True
    assert ctl.mpv.props["sid"] == en
    assert ctl.select_subtitle(zh, expected_media=str(video))[0] is True
    assert ctl.mpv.props["sid"] == zh


def test_old_media_menu_cannot_select_a_reused_track_id(movie):
    ctl, video, sub = movie
    sid = ctl.mpv.add_track(sub)
    ctl.mpv.props["path"] = "new.mp4"
    assert ctl.select_subtitle(sid, expected_media=str(video))[0] is False
    assert ctl.mpv.commands == []


def test_switch_discards_pending_automatic_subtitle(movie):
    ctl, video, _ = movie
    ctl._load_local_subtitle(str(video))
    ctl._playback_gen += 1
    ctl._current = {"first_url": "https://source.test/new.mp4"}
    ctl._apply_pending_local_subtitle()
    assert ctl._pending_local_subtitle is None
    assert ctl.mpv.adds == []


def test_no_subtitle_is_silent_and_does_not_touch_mpv(movie):
    ctl, video, sub = movie
    sub.unlink()
    ctl.mpv = Mock()
    assert ctl._load_local_subtitle(str(video)) is None
    ctl.mpv.assert_not_called()
    assert ctl.mpv.method_calls == []


@pytest.mark.parametrize("response", [None, {"error": "property unavailable"}])
def test_track_selection_error_is_not_success(movie, response):
    ctl, _, sub = movie
    sid = ctl.mpv.add_track(sub)
    ctl.mpv.set_sid = Mock(return_value=response)
    assert ctl.select_subtitle(sid)[0] is False
    assert ctl.mpv.props["sid"] is False
