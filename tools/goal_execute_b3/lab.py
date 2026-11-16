"""Owned temporary resources and real business objects; no player discovery."""
from contextlib import ExitStack
from pathlib import Path
import json
import os
import random
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid
import wave


class LocalSource:
    name = "b3-local-fixture"
    display_name = "Generated local fixture"

    def __init__(self, paths):
        from app.music.online.models import OnlineTrack
        self.paths = tuple(paths)
        self.tracks = [OnlineTrack(self.name, str(i), "Generated " + str(i), duration_seconds=30)
                       for i in range(len(paths))]
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.closed = False

    def search(self, *args, **kwargs):
        raise AssertionError("search_not_used_by_chart_plan")

    def get_top_lists(self):
        self.calls.append("catalog")
        return {self.name: [{"title": "Generated", "lists": [{"id": "lab", "title": "热门测试榜"}]}]}

    def get_top_list_detail(self, source, list_id, page=1):
        self.calls.append("detail")
        assert source == self.name and list_id == "lab"
        return {"tracks": self.tracks, "is_end": True}

    def get_play_url(self, track):
        self.calls.append("resolve")
        self.entered.set()
        if not self.release.wait(2):
            raise TimeoutError("bounded_local_fixture_wait")
        assert not self.closed
        return str(self.paths[int(track.track_id)])

    def get_lyric(self, track):
        return None

    def close(self):
        self.closed = True
        self.release.set()


class Lab:
    """Only newly created paths, instance-scoped IPC and bus are accepted.

    manual_revision/action_gate belong to this LAB, not to production. They make
    explicit where a future atomic owner API is still required.
    """
    def __init__(self, root, binary):
        self.root, self.binary = Path(root).resolve(), Path(binary).resolve()
        if not self.binary.is_file() or os.name != "nt":
            raise ValueError("existing_windows_mpv_required")
        if self.root.exists():
            raise ValueError("new_empty_lab_root_required")
        self.root.mkdir(parents=True)
        self.media = self.root / "media"
        self.media.mkdir()
        self.manual_revision = 0
        self.action_gate = threading.Lock()
        self.movie = "stopped"
        self.movie_calls = 0
        self.active = False
        self.stack = ExitStack()
        self.before_threads = set(threading.enumerate())
        self.source = None
        self.members = {}
        self.catalog_at = 0
        self.refuse_collection_once = False  # Explicit pre-business fault probe.
        self.movie_process = self.movie_port = None
        self.cleanup = {}

    def __enter__(self):
        from app.core.command_bus import CommandBus
        from app.core.event_bus import EventBus
        from app.core.state_store import StateStore
        from app.core.logger import Logger
        from app.music.mpv import MpvController
        from app.music.controller import MusicController
        from tools.goal_shadow.mpv_read_port import MpvReadPort

        root = self.root
        binary = self.binary
        self.log = Logger(str(root / "logs"))
        self.bus, self.events, self.state = CommandBus(), EventBus(), StateStore()
        self.stack.enter_context(patch("app.music.controller.get_command_bus", return_value=self.bus))
        self.stack.enter_context(patch("app.music.controller.get_event_bus", return_value=self.events))
        self.stack.enter_context(patch("app.music.controller.get_state_store", return_value=self.state))
        self.stack.enter_context(patch("app.music.controller.logger", self.log))
        self.stack.enter_context(patch("app.music.controller.LxMusicClient", return_value=SimpleNamespace(is_connected=lambda: False)))
        self.stack.enter_context(patch.object(MusicController, "_load_trace_enabled", return_value=False))

        class SilentMpv(MpvController):
            def __init__(self):
                super().__init__(str(binary), ipc_pipe=r"\\.\pipe\starlight-b3-" + uuid.uuid4().hex)
                self.loads = []
                self.fail_before_send = False
                self.unknown_after_send = False
                self.pause_commands = []

            def start(self, **kwargs):
                original = subprocess.Popen
                def launch(args, **options):
                    process = original([*args, "--no-config", "--load-scripts=no", "--ao=null", "--vo=null",
                                     "--volume=0", "--tls-verify=yes", "--audio-display=no",
                                     "--config-dir=" + str(root / "config")], **options)
                    import psutil
                    (root / "owned-process.json").write_text(json.dumps(dict(pid=process.pid,
                        created=psutil.Process(process.pid).create_time(), binary=str(binary),
                        pipe=self.ipc_pipe)), encoding="utf8")
                    return process
                with patch("app.music.mpv.subprocess.Popen", side_effect=launch):
                    return super().start(taskkill=False, pipe_timeout=3, prop_timeout=.2)

            def loadfile(self, path, mode="replace"):
                resolved = Path(path).resolve()
                if not resolved.is_relative_to(root / "media"):
                    raise ValueError("only_generated_local_media_allowed")
                if self.fail_before_send:
                    self.loads.append({"sent": False, "ack": "rejected_before_ipc"})
                    return {"error": "lab_rejected_before_ipc"}
                result = super().loadfile(str(resolved), mode)
                self.loads.append({"sent": True, "ack": "unknown" if self.unknown_after_send else
                                   (result or {}).get("error", "unknown")})
                return None if self.unknown_after_send else result

            def set_property(self, name, value):
                result = super().set_property(name, value)
                if name == "pause":
                    self.pause_commands.append((value, (result or {}).get("error", "unknown")))
                return result

        self.mpv = SilentMpv()
        try:
            self.ctrl = MusicController(dict(database=str(root / "music.db"), scan_dirs=[],
                download_dir=str(root / "downloads"), mpv_path=str(binary), default_volume=0,
                extensions=[".wav"], play_mode="sequential"), mpv_adapter=self.mpv)
            self.stack.enter_context(patch.object(self.ctrl.scanner, "start_highlight_background", return_value=None))
            self.ctrl.set_movie_pauser(self._movie_absent)
            assert self.ctrl.start(), "isolated_mpv_start_failed"
            self.process = self.mpv._process
            self.port = MpvReadPort(self.mpv.ipc_pipe)
            self.active = True
            # Initializes only the newly created preference file.
            self.ctrl.music_memory.profile()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _movie_absent(self):
        self.movie_calls += 1
        return self.movie == "stopped"  # Observation only, never pauses a film.

    def generate(self, name="generated", seconds=30, broken=False):
        path = self.media / (name + ".wav")
        assert not path.exists()
        if broken:
            path.write_bytes(b"not a valid media stream")
        else:
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\0\0" * int(8000 * seconds))
        return path

    def add_collection(self, path):
        row = dict(title="Generated collection", path=str(path), filename=path.name, format="wav", duration=30)
        row["id"] = self.ctrl.db.insert_song(row)
        playlists = self.ctrl.db.get_playlists()
        pid = playlists[0]["id"] if playlists else self.ctrl.db.create_playlist("我喜欢的音乐")
        assert self.ctrl.db.add_to_playlist(pid, dict(row, source_type="local"))
        from tools.goal_shadow.readonly import opaque
        for item in self.ctrl.db.get_playlist_songs(pid):
            self.members[opaque((pid, item["id"]))] = item
        return row, pid

    def charts(self, paths):
        from app.music.online.manager import OnlineSourceManager
        self.source = LocalSource(paths)
        manager = OnlineSourceManager(max_workers=2, request_timeout=1)
        manager.register(self.source)
        assert manager.get_top_lists()
        self.catalog_at = time.monotonic()
        self.ctrl.set_online_manager(manager)
        self.ctrl.recommendations.rng = random.Random(17)
        return self.source

    def manual(self, function):
        with self.action_gate:
            self.manual_revision += 1
            return function()

    def start_movie_probe(self, path):
        """Second owned MPV represents a film player; generated audio, no video UI."""
        from tools.goal_shadow.mpv_read_port import MpvReadPort
        import psutil
        if not Path(path).resolve().is_relative_to(self.media) or self.movie_process is not None:
            raise ValueError("only_one_generated_movie_state_probe")
        pipe = r"\\.\pipe\starlight-b3-movie-"+uuid.uuid4().hex
        self.movie_process = subprocess.Popen([str(self.binary), "--no-config", "--load-scripts=no",
            "--ao=null", "--vo=null", "--idle=yes", "--volume=0", "--no-terminal",
            "--input-ipc-server="+pipe, str(path)], creationflags=subprocess.CREATE_NO_WINDOW)
        (self.root / "owned-movie-process.json").write_text(json.dumps(dict(pid=self.movie_process.pid,
            created=psutil.Process(self.movie_process.pid).create_time(), binary=str(self.binary), pipe=pipe)))
        self.movie_port = MpvReadPort(pipe)
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            try:
                props = self.movie_port.sample(time.monotonic()+.3)
                if (props.get("idle-active") is False and props.get("pause") is False
                        and type(props.get("time-pos")) in (int, float)
                        and type(props.get("time-pos-after")) in (int, float)
                        and props["time-pos-after"] > props["time-pos"]):
                    self.movie = "playing"
                    return
            except (TimeoutError, OSError):
                pass
            time.sleep(.01)
        raise RuntimeError("owned_movie_probe_not_ready")

    def observe_movie(self):
        if self.movie_port is None:
            return self.movie
        try:
            props = self.movie_port.sample(time.monotonic()+.25)
            if props.get("idle-active") is True:
                return "stopped"
            if props.get("pause") is True:
                return "paused"
            if props.get("time-pos-after", -1) > props.get("time-pos", 0):
                return "playing"
        except (TimeoutError, OSError, TypeError):
            pass
        return "unknown"

    def __exit__(self, *exc):
        self.active = False
        if self.source:
            self.source.release.set()
        try:
            if self.movie_port:
                self.movie_port.close()
            if self.movie_process:
                self.movie_process.terminate()
                try:
                    self.movie_process.wait(2)
                except subprocess.TimeoutExpired:
                    self.movie_process.kill()
                    self.movie_process.wait(2)
            if hasattr(self, "port"):
                self.port.close()
            if hasattr(self, "ctrl"):
                self.ctrl.stop()
                self.ctrl.history_recorder.stop()
                if self.ctrl.downloader:
                    self.ctrl.downloader._pool.shutdown(wait=True, cancel_futures=True)
            self.events.shutdown(timeout=1)
            deadline = time.monotonic() + 3
            for thread in set(threading.enumerate()) - self.before_threads:
                if thread is not threading.current_thread():
                    thread.join(max(0, deadline - time.monotonic()))
            remaining = [t.name for t in set(threading.enumerate()) - self.before_threads if t.is_alive()]
            self.cleanup = dict(threads_remaining=remaining, process_exited=not self.mpv.is_running())
            assert self.movie_process is None or self.movie_process.poll() is not None
            if hasattr(self, "ctrl"):
                self.ctrl.db.close()
            assert not remaining, remaining
            assert not self.mpv.is_running()
        finally:
            self.stack.close()
            self.log.close()
