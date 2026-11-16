"""Only generated WAV + new MPV process/IPC. Decision code never loads or pauses."""
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid
import wave

import pytest

from app.music.queue import PlayQueue
from app.voice.media_context import MediaContext
from tools.goal_shadow.contracts import Action, Goal
from tools.goal_shadow.decision import decide
from tools.goal_shadow.mpv_read_port import MpvReadPort
from tools.goal_shadow.readonly import LiveReadAdapter

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("paused,expected", [(False, Action.NO_OP), (True, Action.RESUME_MUSIC)])
def test_independent_read_port_observes_real_decoder(tmp_path, paused, expected):
    binary = Path(os.environ["STARLIGHT_TEST_MPV"])
    assert os.name == "nt" and binary.is_file()
    media = tmp_path/"生成隔离音频.wav"
    with wave.open(str(media), "wb") as output:
        output.setparams((1,2,8000,0,"NONE","not compressed"))
        output.writeframes(b"\0\0"*8000*20)
    pipe = r"\\.\pipe\starlight-b1-"+uuid.uuid4().hex
    process = subprocess.Popen([str(binary), "--no-config", "--ao=null", "--vo=null", "--idle=yes",
        "--volume=0", "--audio-display=no", "--no-terminal", "--pause="+("yes" if paused else "no"),
        "--input-ipc-server="+pipe, str(media)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    port = MpvReadPort(pipe)
    queue = PlayQueue()
    song = dict(id=1, path=str(media))
    queue.add(song)
    queue.play_at(0)
    controller = SimpleNamespace(_lock=threading.Lock(), _play_request_id=1, _current_song=song,
        _is_switching_track=False, queue=queue, db_path=tmp_path/"not-created.db")
    context = MediaContext(SimpleNamespace())
    adapter = LiveReadAdapter(controller, port, movie_reader=lambda: context.sample("movie"))
    try:
        deadline = time.monotonic()+3
        ready = False
        while time.monotonic()<deadline:
            try:
                props = port.sample(time.monotonic()+.2)
                position = props.get("time-pos")
                ready = (props.get("idle-active") is False and props.get("pause") is paused
                         and type(position) in (int, float) and position >= 0
                         and (paused or props.get("time-pos-after", position) > position))
                if ready:
                    break
            except (OSError, TimeoutError):
                pass
            time.sleep(.02)
        assert ready, "isolated decoder did not become ready"
        before = queue.snapshot(song)
        world = adapter.capture(budget=1)
        result = decide(Goal(), world, now=time.monotonic(), current_generation=adapter.generation())
        assert result.action is expected, (world, result)
        assert result.goal_satisfied is (not paused)
        assert not result.execution_performed
        assert before == queue.snapshot(song)
        assert port.get_property("pause", time.monotonic()+.2) is paused
        assert process.poll() is None
        assert not (tmp_path/"not-created.db").exists()
    finally:
        port.close()
        process.terminate()
        try:
            process.wait(2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(2)
        assert process.poll() is not None


def test_query_port_rejects_non_property_actions():
    port = MpvReadPort(r"\\.\pipe\starlight-b1-not-connected")
    with pytest.raises(ValueError, match="property_not_allowlisted"):
        port.get_property("loadfile", time.monotonic()+.1)
    assert port._stream is None
