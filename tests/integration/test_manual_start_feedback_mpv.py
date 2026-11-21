"""Generated media, the normal music panel slot and independently owned MPV."""
from unittest.mock import Mock

import pytest

from app.music.online.models import OnlineTrack
from tests.ui.test_music_search_reliability import panel, qapp, wait_until  # noqa: F401
from tests.integration.test_music_eof_mpv import native  # noqa: F401


@pytest.mark.integration
def test_online_click_waits_for_generated_media_to_advance(native, panel):
    p, _, _ = panel
    ctrl = native.ctrl
    p._music = ctrl
    song = native.songs[0]
    track = OnlineTrack("test", "generated", song["title"], duration_seconds=3)
    ctrl.online_manager = Mock()
    ctrl.online_manager.get_play_url.return_value = {"url":song["path"]}
    seen = []
    p._manual_play_status.connect(lambda _, epoch, stage: seen.append(stage))
    p._play_async(ctrl.play_online, track, resolving=True, title=track.title)
    wait_until(lambda: "started" in seen, timeout=6)
    assert seen == ["loading", "accepted", "started"]
    assert p.lbl_status.text() == "正在播放：" + track.title
    assert len(native.mpv.loads) == 1
    assert native.mpv.get_property("time-pos", timeout=.3) > 0
