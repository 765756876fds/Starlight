"""Generated silent WAV -> real MPV EOF -> real controller queue advance.

Windows only; uses the bundled MPV or STARLIGHT_TEST_MPV. Null audio output,
unique IPC, no user configuration, network, real music library or speakers.
"""
import json
import os
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.music.controller import MusicController
from app.music.mpv import MpvController
from app.music.queue import PlayMode

pytestmark = pytest.mark.integration


def wait(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), "bounded native MPV/queue wait expired"


@pytest.fixture
def native(tmp_path):
    binary = Path(os.environ.get("STARLIGHT_TEST_MPV", Path(__file__).resolve().parents[2] / "third_party/mpv/mpv.exe"))
    assert os.name == "nt" and binary.is_file(), "this native probe requires existing Windows MPV"
    original_popen = subprocess.Popen

    class SilentMpv(MpvController):
        def __init__(self):
            super().__init__(str(binary), ipc_pipe=r"\\.\pipe\starlight-eof-" + uuid.uuid4().hex)
            self.loads = []

        def start(self, **kwargs):
            def launch(args, **options):
                return original_popen([*args, "--no-config", "--ao=null", "--volume=0", "--audio-display=no"], **options)
            with patch("app.music.mpv.subprocess.Popen", side_effect=launch):
                return super().start(taskkill=False, pipe_timeout=3, prop_timeout=.3)

        def loadfile(self, path, mode="replace"):
            response = super().loadfile(path, mode)
            self.loads.append((path, response))
            return response

    mpv = SilentMpv()
    with patch("app.music.controller.LxMusicClient") as lx:
        lx.return_value.is_connected.return_value = False
        ctrl = MusicController(dict(database=str(tmp_path / "music.db"), scan_dirs=[], extensions=[".wav"],
                                    mpv_path=str(binary), default_volume=0, highlight_enabled=False), mpv_adapter=mpv)
    events, finish = [], threading.Event()
    observer, reader = None, None
    try:
        assert ctrl.start()
        ctrl.set_movie_pauser(lambda: True)
        ctrl.highlight_enabled = False
        observer = open(mpv.ipc_pipe, "r+b", buffering=0)
        observer.write(b'{"command":["get_property","idle-active"],"request_id":99}\n')

        def read_events():
            while not finish.wait(.01):
                if MpvController._pipe_available(SimpleNamespace(_pipe=observer)) > 0:
                    try:
                        row = json.loads(observer.readline())
                    except (ValueError, OSError):
                        break
                    if row.get("event") == "end-file":
                        events.append(row)

        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        songs = []
        for name in ("A", "B"):
            path = tmp_path / (name + ".wav")
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\0\0" * 24000)  # 3 seconds of generated silence
            song = dict(title="生成音频" + name, path=str(path), filename=path.name,
                        format="wav", duration=3.0)
            song["id"] = ctrl.db.insert_song(song)
            songs.append(song)
        yield SimpleNamespace(ctrl=ctrl, mpv=mpv, songs=songs, events=events)
    finally:
        finish.set()
        if reader:
            reader.join(1)
            assert not reader.is_alive()
        if observer:
            observer.close()
        ctrl.stop()
        ctrl.db.close()
        assert not mpv.is_running()


@pytest.mark.parametrize("mode", [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE])
def test_real_eof_advances_or_repeats_according_to_the_existing_mode(native, mode):
    s = native
    s.ctrl.queue.add_all(s.songs)
    s.ctrl.queue.set_play_mode(mode)
    s.ctrl.next()
    wait(lambda: len(s.mpv.loads) >= 2)
    assert all(response and response.get("error") == "success" for _, response in s.mpv.loads)
    wait(lambda: any(row.get("reason") == "eof" for row in s.events))
    if mode == PlayMode.REPEAT_ONE:
        assert s.mpv.loads[0][0] == s.mpv.loads[1][0]
    else:
        assert s.mpv.loads[0][0] != s.mpv.loads[1][0]
    if mode == PlayMode.SEQUENTIAL:
        wait(lambda: s.ctrl._current_song is None)
        assert len(s.mpv.loads) == 2 and s.ctrl._running
        assert s.mpv.get_property("idle-active") is True
