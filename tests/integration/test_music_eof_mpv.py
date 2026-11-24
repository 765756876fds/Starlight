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
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.music.controller import MusicController
from app.music.mpv import MpvController
from app.music.online.models import OnlineTrack
from app.music.queue import PlayMode
from app.ui.panels.music_panel import MusicPanel

pytestmark = pytest.mark.integration


def wait(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), "bounded native MPV/queue wait expired"


@pytest.fixture
def native(tmp_path, monkeypatch, request):
    # Each standalone controller owns its event subscribers and their database lifetime.
    # The widgets and controller still use the same real bus throughout this case.
    from app.core import command_bus, event_bus, state_store
    bus = event_bus.EventBus()
    monkeypatch.setattr(event_bus, "_event_bus", bus)
    monkeypatch.setattr(command_bus, "_command_bus", command_bus.CommandBus())
    monkeypatch.setattr(state_store, "_state_store", state_store.StateStore())
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
        durations = getattr(request, "param", (3, 3))
        for name, duration in zip(("A", "B"), durations):
            path = tmp_path / (name + ".wav")
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\0\0" * (8000 * duration))
            song = dict(title="生成音频" + name, path=str(path), filename=path.name,
                        format="wav", duration=float(duration))
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
        bus.shutdown()
        ctrl.db.close()
        assert not mpv.is_running()


@pytest.mark.parametrize("mode", [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE])
def test_real_eof_advances_or_repeats_according_to_the_existing_mode(native, mode, capsys):
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
    diagnostics = capsys.readouterr().out
    assert "music_playback stage=loadfile result=confirmed media=local" in diagnostics
    assert "queue_size=2" in diagnostics and "request_id=" in diagnostics
    assert "end-file reason=eof" in diagnostics


@pytest.mark.parametrize("native", [(3, 12)], indirect=True, ids=["natural-eof-and-seek"])
def test_rank_to_local_double_click_continues_in_local_library(native, tmp_path):
    s = native
    app = QApplication.instance() or QApplication([])
    panel = MusicPanel(s.ctrl)
    panel.resize(850, 680)
    panel.show()
    playlist = list(panel._local_songs)
    assert len(playlist) == 2
    for index, row in enumerate(playlist):
        Path(row["path"]).with_suffix(".lrc").write_text(
            f"[00:00.00]歌曲 {index} 开始\n[00:00.70]歌曲 {index} 推进\n", "utf-8")
    chart_audio = tmp_path / "chart.wav"
    chart_audio.write_bytes(Path(playlist[1]["path"]).read_bytes())
    chart = [OnlineTrack("fixture", str(i), f"排行榜{i}") for i in range(2)]
    s.ctrl.online_manager = SimpleNamespace(
        get_play_url=lambda track, **kw: {"url": str(chart_audio)},
        close=lambda: None,
    )
    s.ctrl._resolver = SimpleNamespace()
    s.ctrl.queue.set_play_mode(PlayMode.REPEAT_ALL)

    def pump_until(predicate):
        deadline = time.monotonic() + 9
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(20)
        assert predicate()

    try:
        panel._play_popular_song(chart, 0)
        pump_until(lambda: s.ctrl.get_current_song() and s.ctrl.get_current_song()["title"] == "排行榜0")
        panel.list_local.itemDoubleClicked.emit(panel.list_local.item(0))
        pump_until(lambda: s.ctrl.get_current_song()["title"] == playlist[0]["title"])
        panel.btn_playing_lyrics.click()
        pump_until(lambda: panel.playing_lyrics.active_line == 1)
        stage = panel.playing_lyrics.stage
        assert stage.isVisible() and stage.row == 1
        assert stage.lines[stage.row][1] == "歌曲 0 推进"
        pump_until(lambda: len(s.mpv.loads) >= 3)
        assert [path for path, _ in s.mpv.loads[:3]] == [
            str(chart_audio), playlist[0]["path"], playlist[1]["path"],
        ]
        assert [row["title"] for row in s.ctrl.get_queue()] == [row["title"] for row in playlist]
        assert all(response and response.get("error") == "success" for _, response in s.mpv.loads[:3])
        assert any(row.get("reason") == "eof" for row in s.events)
        pump_until(lambda: panel.playing_lyrics.title.text() == playlist[1]["title"])
        pump_until(lambda: stage.lines and stage.lines[0][1] == "歌曲 1 开始")
        # A ends naturally at 3 s. Give B time to verify its decoded position
        # even when Qt is busy receiving the EOF and new-track notifications.
        # Seek the naturally selected song via its normal control, then compare
        # the visible cue and stage clock with MPV, inside that same cue.
        s.ctrl.seek(1.35, mode="absolute")

        def second_song_advancing():
            position = s.mpv.get_property("time-pos")
            return (isinstance(position, (int, float)) and .8 < position < 2.7
                    and s.ctrl.get_current_song()["title"] == playlist[1]["title"]
                    and abs(position - stage.position) < .3
                    and stage.row == 1 and stage.lines[stage.row][1] == "歌曲 1 推进")
        pump_until(second_song_advancing)
    finally:
        panel.close()
