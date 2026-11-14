"""Real, isolated MPV decodes generated audio; chart metadata is explicitly a fixture."""
import threading
from unittest.mock import patch

import pytest

from app.core.command_bus import CommandBus
from app.core.operation_result import OperationStatus
from app.music.online.models import OnlineTrack
from app.music.queue import PlayMode
from app.voice.runtime import VoiceRuntime
from tests.integration.test_music_eof_mpv import native as native
from tests.integration.test_music_eof_mpv import wait

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE])
def test_voice_recommendation_real_mpv_and_natural_continuation(native, mode):
    s = native
    tracks = [OnlineTrack("generated", str(i), row["title"], "生成测试", duration_seconds=3)
              for i, row in enumerate(s.songs)]
    class Sources:
        calls = 0
        def get_top_lists(self):
            self.calls += 1
            return {"generated": [{"lists": [{"id": "fixture", "title": "生成音频热门榜（测试）"}]}]}
        def get_top_list_detail(self, *args, **kwargs):
            return {"tracks": tracks}
        def get_play_url(self, track, **kwargs):
            return {"url": s.songs[int(track.track_id)]["path"]}
        def close(self):
            pass
    source = Sources()
    s.ctrl.online_manager = source
    s.ctrl._resolver = object()  # Direct ID URL path does not invoke resolver search.
    s.ctrl.remember_cover = lambda **kwargs: None
    s.ctrl.queue.set_play_mode(mode)
    # Real runtime -> real asynchronous CommandBus -> registered real controller.
    bus = CommandBus()
    s.ctrl.command_bus = bus
    s.ctrl._register_commands()
    rt = VoiceRuntime({"asr": {}, "intent": {}})
    rt.command_bus = bus
    rt._enqueue_speak = lambda *a, **k: None
    results, done = [], threading.Event()
    original = s.ctrl.recommend_music
    def observed(**kwargs):
        result = original(**kwargs)
        results.append(result)
        done.set()
        return result
    with patch.object(s.ctrl, "recommend_music", observed):
        rt._execute_intent(rt.intent_router.parse("我想听歌了"), "我想听歌了")
        assert done.wait(6)
    assert results[0].status is OperationStatus.CONFIRMED
    initial = s.mpv.loads[0][0]
    wait(lambda: len(s.mpv.loads) >= 2, seconds=10)
    assert source.calls == 1  # EOF consumes the existing collection, no new recommendation.
    assert any(event.get("reason") == "eof" for event in s.events)
    if mode == PlayMode.REPEAT_ONE:
        assert s.mpv.loads[1][0] == initial
    else:
        assert s.mpv.loads[1][0] != initial
    assert s.ctrl.music_memory.profile()["artists"] == {}
