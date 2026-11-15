"""Generated local audio, actual MPV; offline URL-source substitute, no network."""

import pytest

from app.core import notifications as notices
from app.core.command_bus import Command, CommandType as C
from app.core.notifications import NotificationCenter, operation_scope
from app.core.operation_result import OperationStatus as S
from app.music.online.models import OnlineTrack
from app.music.queue import PlayMode
from tests.integration.test_music_eof_mpv import native as native, wait

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE])
def test_voice_next_confirms_actual_new_mpv_entry(native, monkeypatch, mode):
    c, player = native.ctrl, native.mpv
    center = NotificationCenter()
    monkeypatch.setattr(notices, "_center", center)
    c.queue.add_all(native.songs)
    c.queue.set_play_mode(mode)
    c.next()
    wait(lambda: (player.get_property("time-pos") or 0) > 0.4)
    operation = center.post("正在切歌", source="voice")
    with operation_scope(operation):
        result = c.command_bus.execute(Command(C.MUSIC_NEXT, source="voice"))
    assert result.status is S.ACCEPTED
    wait(lambda: center.get(operation).status == "success", seconds=2)
    assert len(player.loads) == 2
    assert center.get(operation).title == "已确认下一首开始播放"
    assert len(center.history()) == 1
    assert c.queue.size() == 2 and c.queue.get_play_mode() == mode


def test_offline_online_queue_source_uses_real_mpv_confirmation(native, monkeypatch):
    from unittest.mock import Mock
    c, player = native.ctrl, native.mpv
    center = NotificationCenter()
    monkeypatch.setattr(notices, "_center", center)
    c.queue.add_all([native.songs[0], {"source_type": "online", "source_name": "offline",
                                     "track_id": "B", "title": "生成在线替身"}])
    c._resolver = Mock()
    c.online_manager = Mock(get_play_url=Mock(return_value={"url": native.songs[1]["path"]}))
    c.next()
    wait(lambda: (player.get_property("time-pos") or 0) > 0.4)
    operation = center.post("正在切歌", source="voice")
    with operation_scope(operation):
        value = c.command_bus.execute(Command(C.MUSIC_NEXT, source="voice"))
    assert value.status is S.ACCEPTED
    wait(lambda: center.get(operation).status == "success", seconds=2)
    assert player.loads[-1][0] == native.songs[1]["path"]
    assert c.online_manager.get_play_url.call_count == 1
    assert isinstance(c.online_manager.get_play_url.call_args.args[0], OnlineTrack)
    c._resolver.resolve.assert_not_called()
