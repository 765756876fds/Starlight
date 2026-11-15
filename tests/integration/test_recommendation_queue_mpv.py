"""Generated local WAV served as an isolated source; no real online platform claim."""
import threading

from app.core.operation_result import OperationStatus
from app.music.online.models import OnlineTrack
from app.music.queue import PlayMode
from tests.integration.test_music_eof_mpv import native as native, wait
from tests.unit.test_music_recommendation import Sources

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE])
def test_real_recommendation_load_and_eof_keep_chart_collection(native, mode):
    s = native
    source = Sources()
    source.tracks = [OnlineTrack("fixture", str(index), row["title"], "生成测试",
                                duration_seconds=3) for index, row in enumerate(s.songs)]
    def play_url(track, **kwargs):
        return {"url": s.songs[int(track.track_id)]["path"]}
    source.get_play_url = play_url
    # The native fixture's manager belongs to this isolated controller, never the user app.
    if s.ctrl.online_manager is not None:
        s.ctrl.online_manager.close()
    s.ctrl.online_manager = source
    s.ctrl._resolver = object()
    resolved = threading.Event()
    resolve = s.ctrl._resolve_online_worker
    def complete(*args):
        try:
            return resolve(*args)
        finally:
            resolved.set()
    s.ctrl._resolve_online_worker = complete
    s.ctrl.queue.set_play_mode(mode)
    result = s.ctrl.recommend_music()
    assert result.status is OperationStatus.CONFIRMED
    assert s.ctrl.queue.size() == 2 and s.ctrl.queue.get_current_index() == 0
    first = s.mpv.loads[0][0]
    session = s.ctrl.recommendations.session
    reads = source.details
    wait(lambda: len(s.mpv.loads) >= 2)
    wait(lambda: any(row.get("reason") == "eof" for row in s.events))
    wait(lambda: not s.ctrl._is_switching_track)
    assert all(reply and reply.get("error") == "success" for _, reply in s.mpv.loads)
    assert (s.mpv.loads[1][0] == first) is (mode == PlayMode.REPEAT_ONE)
    assert s.ctrl.queue.size() == 2 and s.ctrl.recommendations.session is session
    assert source.details == reads
    # loadfile acknowledgement precedes history writes. Finish this test's worker
    # before the fixture closes its private database and player.
    assert resolved.wait(2)
