import threading
from unittest.mock import Mock

import pytest

from app.music.controller import MusicController
from app.music.online.chorus import ChorusLookup, Status, resolve_chorus
from app.music.online.models import OnlineTrack

DIGEST = "f1dba60b3468d75b75791b97401ccfc3"


def recording(platform, branch):
    t = OnlineTrack(platform, "2565477", "公开样本", duration_seconds=240,
                    extra={"_raw": {"id": "2565477"}})
    return t, {"playback_metadata": dict(branch=branch, status="matched", id=t.track_id,
                                         media_id=DIGEST)}


def test_verified_qishui_point_not_interval():
    track, proof = recording("开心汽水", "qishui.play")
    proof["playback_metadata"].update(start_ms=50688, end_ms=None, duration_ms=240010)
    decision = resolve_chorus(track, proof, 5)
    assert decision.status == Status.POINT and decision.marker.start == 50.688
    assert decision.marker.end is None and decision.marker.fits(240.01)
    assert not decision.marker.fits(60)
    proof["playback_metadata"]["status"] = "limited_range"
    assert resolve_chorus(track, proof, 5).status == Status.PARTIAL


@pytest.mark.parametrize("field,value", [("id","9"),("branch","kugou.direct"),("start_ms",True),
                                         ("start_ms",240010),("duration_ms",255000),("end_ms",1)])
def test_wrong_qishui_evidence(field, value):
    track, proof = recording("开心汽水", "qishui.play")
    proof["playback_metadata"].update(start_ms=50688, duration_ms=240010)
    proof["playback_metadata"][field] = value
    assert not resolve_chorus(track, proof, 5).marker


@pytest.mark.parametrize("platform,branch,reply", [
    ("酷狗音乐","kugou.direct", {"status":1, "data":[{"hash":DIGEST.upper(),"audio_id":"other",
                                                      "start_time":"81200","end_time":"135000"}]}),
    ("网易音乐","netease.direct", {"code":200, "data":[{"id":2565477,"startTime":81200,"endTime":135000}]}),
])
def test_lookup_cache_is_versioned_and_rebinds_only_epoch(platform,branch,reply):
    track, proof = recording(platform,branch)
    reader, clock = Mock(return_value=reply), Mock(return_value=0)
    lookup = ChorusLookup(reader,clock=clock)
    a = lookup.after_start(track,proof,7).marker
    b = lookup.after_start(track,proof,8).marker
    assert (a.start,a.end,b.start,b.epoch)==(81.2,135,81.2,8) and a is not b
    assert reader.call_count == 1
    clock.return_value = 301
    assert lookup.after_start(track,proof,9).marker and reader.call_count == 2
    proof["playback_metadata"]["media_id"] = "a"*32
    lookup.after_start(track,proof,10)
    assert reader.call_count == 3


def test_hash_cannot_be_replaced_by_audio_id_and_no_qq_trial():
    t,p=recording("酷狗音乐","kugou.direct")
    for row in ({"audio_id":t.track_id},{"hash":"e"*32}):
        assert resolve_chorus(t,p,9,reply={"status":1,"data":[row]}).status == Status.MISMATCH
    p["playback_metadata"]["media_id"]=t.track_id
    reader=Mock()
    assert not ChorusLookup(reader).after_start(t,p,1).marker
    reader.assert_not_called()
    t.source_name="QQ音乐"
    assert not resolve_chorus(t,p,2,reply={"startTime":3,"endTime":10}).marker


def test_timeout_stale_and_once_per_play():
    t,p=recording("网易音乐","netease.direct")
    ctrl=MusicController.__new__(MusicController)
    ctrl._platform_chorus=None
    ctrl._running=True
    ctrl._user_stopped=ctrl._is_switching_track=False
    ctrl._play_request_id=ctrl._last_loadfile_epoch=7
    ctrl._lock=threading.Lock()
    ctrl._current_song={"source_name":t.source_name,"track_id":t.track_id,"path":"A"}
    ctrl._pending_chorus=(7,t,p)
    props={"path":"A","duration":30,"time-pos":1}
    ctrl.mpv=Mock()
    ctrl.mpv.get_property.side_effect=lambda name,timeout:props[name]
    started,release=threading.Event(),threading.Event()
    def delayed(*_):
        started.set()
        assert release.wait(3)
        return {"code":200,"data":[{"id":int(t.track_id),"startTime":10000,"endTime":20000}]}
    reader=Mock(side_effect=delayed)
    ctrl._chorus_lookup=ChorusLookup(reader)
    assert ctrl.observe_chorus() is None
    props["duration"]=240
    props["time-pos"]=0
    assert ctrl.observe_chorus() is None
    reader.assert_not_called()
    props["time-pos"]=1
    worker=threading.Thread(target=ctrl.observe_chorus)
    worker.start()
    try:
        assert started.wait(2)
        assert ctrl.observe_chorus() is None
        ctrl._play_request_id+=1
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and ctrl._platform_chorus is None
    reader.assert_called_once()
    assert ChorusLookup(Mock(side_effect=TimeoutError)).after_start(t,p,1).status == Status.FAILED



def test_unrelated_resolver_cannot_attest_recording():
    from app.music.online.manager import OnlineSourceManager
    from app.music.online.sources.js_source import JSMusicSource
    proof = {"url": "file", "kuwo_rid": "7", "playback_metadata": {"bad": True}}
    normalize = OnlineSourceManager._normalize_play_result
    assert normalize(proof, object()) == {"url": "file"}
    assert normalize(proof, JSMusicSource.__new__(JSMusicSource)) is proof
