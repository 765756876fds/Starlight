import pytest

"""Behavior contract for the music quality menu. Always use owned media."""
from app.music.playback_quality import media_facts, rendition_order
from app.music.online.content_choices import equivalent_versions
from app.music.online.models import OnlineTrack

def test_decoder_pcm_is_never_encoded_bit_depth():
    track={"codec":"flac","demux-samplerate":96000,"demux-channel-count":2,"format-name":"s32"}
    facts=media_facts(track,{"samplerate":48000,"format":"s16","channel-count":2},None,None)
    assert facts["codec"]=="FLAC"
    assert facts["sample_rate"]==96000
    assert facts["bits"] is None
    assert facts["output_rate"]==48000
    assert facts["output_format"]=="s16"
    assert facts["channels"]==2

def test_unknown_does_not_inherit_requested_tier():
    facts=media_facts({}, {}, None, None)
    assert all(value is None for value in facts.values())

def test_lossy_media_keeps_encoded_rate_and_unknown_depth():
    track={"codec":"aac","demux-samplerate":44100,"demux-bitrate":128175,"demux-channel-count":2}
    facts=media_facts(track,{},None,53000)
    assert facts["codec"]=="AAC" and facts["bitrate"]==128175
    assert facts["bits"] is None and facts["sample_rate"]==44100

def test_only_real_equivalent_version_is_eligible():
    def song(source,track_id,title="海玻璃",album="潮汐手记",dur=64):
        return OnlineTrack(source,track_id,title,"Starlight Studio",album,dur)
    track=song("A","original");exact=song("B","alias")
    assert list(equivalent_versions(track,[song("B","live","海玻璃 Live"),song("B","cut",dur=30),song("C","uncertain",album=""),exact]))==[exact]

def test_lower_preference_does_not_probe_all_higher_tiers():
    assert rendition_order("standard","lx")==("128k",)
    assert rendition_order("auto","lx")[0]=="flac24bit"
    assert rendition_order("lossless","musicfree")[0]=="super"


def test_lx_requests_only_declared_quality_with_original_recording(tmp_path):
    import json
    import os
    import shutil
    import subprocess
    from pathlib import Path
    import pytest

    node = os.environ.get("NODE_EXE") or shutil.which("node")
    if not node:
        pytest.skip("Node runtime is required for the LX protocol test")
    plugin = tmp_path / "owned.js"
    plugin.write_text('''
      lx.send(lx.EVENT_NAMES.inited,{sources:{fixture:{type:"music",actions:["musicUrl"],qualitys:["320k"]}}});
      lx.on(lx.EVENT_NAMES.request,async ({source,info})=>{
        if(source!=="fixture" || info.type!=="320k" || info.musicInfo.id!=="owned") throw Error("wrong request");
        return "http://127.0.0.1/owned-audio";
      });
    ''',encoding="utf8")
    loader = Path(__file__).resolve().parents[2]/"app/music/online/js_bridge/lib/lx_loader.js"
    js = """(async()=>{const p=await require(process.argv[1]).load(process.argv[2]);
           const item={id:'owned',source:'fixture'};
           console.log(JSON.stringify([await p.getUrl('owned',item,'flac24bit',true),
           await p.getUrl('owned',item,'320k',true)]));})().catch(()=>process.exit(1))"""
    result = subprocess.run([node,"-e",js,str(loader),str(plugin)],capture_output=True,check=True,timeout=5)
    assert json.loads(result.stdout)==[None,{"url":"http://127.0.0.1/owned-audio","quality":"320k"}]


@pytest.mark.parametrize("fragmented", [False, True])
def test_decoder_query_keeps_coalesced_events_and_partial_utf8_reply(fragmented):
    import io
    import json
    from collections import deque
    from app.music.mpv import MpvController

    class Pipe(io.RawIOBase):
        def __init__(self):
            self.chunks = deque()

        def write(self, data):
            request = json.loads(data)
            reply = json.dumps({"request_id": request["request_id"], "data": "已解码"},
                               ensure_ascii=False).encode("utf8") + b"\n"
            cut = reply.index("已".encode("utf8")) + 1
            event = b'{"event":"idle"}\n'
            if fragmented:
                self.chunks.extend([event + reply[:cut], reply[cut:]])
            else:
                self.chunks.append(event + reply)
            return len(data)

        def read(self, size):
            chunk = self.chunks.popleft()
            assert len(chunk) <= size
            return chunk

    player = MpvController("unused")
    player._pipe = pipe = Pipe()
    player._pipe_available = lambda: len(pipe.chunks[0]) if pipe.chunks else 0
    assert player.get_property("current-tracks/audio", timeout=.12) == "已解码"
    assert not player._command_buffer
