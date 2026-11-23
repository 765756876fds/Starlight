"""Generated media, the normal music panel slot and independently owned MPV."""
import wave
from unittest.mock import Mock

import pytest

from app.music.online.models import OnlineTrack
from tests.ui.test_music_search_reliability import panel, qapp, wait_until  # noqa: F401
from tests.integration.test_music_eof_mpv import native  # noqa: F401


@pytest.mark.integration
def test_online_click_waits_for_generated_media_to_advance(native, panel, tmp_path):
    p, _, _ = panel
    ctrl = native.ctrl
    p._music = ctrl
    song = native.songs[0]
    # Keep the generated track alive through multiple native observations.
    clip = tmp_path / "manual-start.wav"
    with wave.open(song["path"], "rb") as source, wave.open(str(clip), "wb") as output:
        output.setparams(source.getparams())
        output.writeframes(source.readframes(source.getnframes()) * 4)
    track = OnlineTrack("test", "generated", song["title"], duration_seconds=12)
    ctrl.online_manager = Mock()
    ctrl.online_manager.get_play_url.return_value = {"url": str(clip)}
    seen = []
    p._manual_play_status.connect(lambda _, epoch, stage: seen.append(stage))
    p._play_async(ctrl.play_online, track, resolving=True, title=track.title)
    wait_until(lambda: "started" in seen, timeout=6)
    assert seen == ["loading", "accepted", "started"]
    receipt = "已开始播放：" + track.title
    assert p.lbl_status.text() == receipt
    assert len(native.mpv.loads) == 1
    assert native.mpv.get_property("time-pos", timeout=.3) > 0

    wait_until(ctrl.is_playing)
    ctrl.toggle()
    assert not ctrl.is_playing()
    assert native.mpv.get_property("pause", timeout=.3) is True
    p._sync_toggle_icon()
    assert p._toggle_icon == "play"
    assert p.lbl_status.text() == receipt

    ctrl.toggle()
    assert ctrl.is_playing()
    assert native.mpv.get_property("pause", timeout=.3) is False
    p._sync_toggle_icon()
    assert p._toggle_icon == "pause"
