"""Real MPV checkpoints using generated media, no user files or global config.

Requires Windows and bundled MPV/FFmpeg (or STARLIGHT_TEST_MPV/FFMPEG).
The null video output verifies IPC/persistence; it does not verify rendering.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from app.movie.movie_controller import MovieController
from app.music.mpv import MpvController
from app.ui.widgets.video_player_window import VideoPlayerWindow

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if sys.platform != "win32":
        pytest.skip("Windows named-pipe MPV integration")
    root = Path(__file__).resolve().parents[2]
    mpv = Path(os.environ.get("STARLIGHT_TEST_MPV", root / "third_party/mpv/mpv.exe"))
    ffmpeg = Path(os.environ.get("STARLIGHT_TEST_FFMPEG", root / "third_party/ffmpeg/ffmpeg.exe"))
    if not mpv.is_file() or not ffmpeg.is_file():
        pytest.skip("MPV and FFmpeg binaries required for native integration")
    directory = tmp_path_factory.mktemp("generated-media")
    video = directory / "生成视频 A.mp4"
    subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=160x90:r=10:d=60", "-an", "-c:v", "mpeg4",
                    "-q:v", "10", str(video)], check=True, timeout=30,
                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
    other = directory / "生成视频 B.mp4"
    shutil.copyfile(video, other)
    return mpv, video, other


class Config:
    def __init__(self, root):
        self.root = root

    def get(self, key, default=None):
        return {"app.data_dir": str(self.root / "data"),
                "movie.history_db": str(self.root / "history.db"),
                "movie.watch_later_dir": str(self.root / "watch"),
                "movie.embed": False, "movie.auto_next": False}.get(key, default)


class Session:
    def __init__(self, root, exe):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.watch = root / "watch"
        self.watch.mkdir(exist_ok=True)
        cfg = root / "mpv-config"
        cfg.mkdir(exist_ok=True)
        self.mpv = MpvController(str(exe), r"\\.\pipe\resume_test_" + uuid.uuid4().hex,
                                 video=True, watch_later_dir=str(self.watch),
                                 extra_video_args=["--config-dir=" + str(cfg), "--load-scripts=no",
                                                   "--vo=null", "--ao=null", "--no-audio",
                                                   "--hwdec=no", "--pause=yes"])
        self.windows = []

    def play(self, path, window=False):
        item = {"vod_name": "生成测试视频", "first_url": str(path), "local_file": True}
        if window:
            w = VideoPlayerWindow(self.ctl)
            # No native rendered child in --vo=null/offscreen mode.
            w._apply_mpv_mouse_passthrough = lambda: None
            self.windows.append(w)
            assert w.play(item)
            w._timer.stop()
            w._hide_timer.stop()
        else:
            assert self.ctl.play(item)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.mpv.get_property("path", timeout=.3)
            pos = self.mpv.get_property("time-pos", timeout=.3)
            if (current and os.path.normcase(os.path.abspath(current)) ==
                    os.path.normcase(str(path.resolve())) and type(pos) in (int, float)):
                assert self.mpv.set_property("pause", True)["error"] == "success"
                return self.mpv.get_property("time-pos", timeout=.3)
            time.sleep(.02)
        pytest.fail("generated media did not become ready")

    def seek(self, pos):
        assert self.mpv.set_property("pause", True)["error"] == "success"
        assert self.mpv._send_command("seek", [pos, "absolute+exact"])["error"] == "success"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            actual = self.mpv.get_property("time-pos", timeout=.3)
            if type(actual) in (int, float) and abs(actual - pos) < .2:
                return actual
            time.sleep(.02)
        pytest.fail("generated media seek failed")

    def cleanup(self):
        # Only our recorded process, never global taskkill or user processes.
        self.mpv.stop_process()
        for w in self.windows:
            w._timer.stop()
            w._hide_timer.stop()
            w._playing = False
            w.close()


@pytest.fixture
def sessions(tmp_path, media, monkeypatch, qapp):
    owned = []

    def make(name="case"):
        s = Session(tmp_path / name, media[0])
        owned.append(s)
        with monkeypatch.context() as m:
            m.setattr("app.movie.movie_controller.MpvController", lambda *a, **kw: s.mpv)
            m.setattr(MovieController, "_subscribe", lambda self: None)
            m.setattr(MovieController, "_register_commands", lambda self: None)
            s.ctl = MovieController(config=Config(s.root), source_manager=Mock())
        return s

    yield make
    for s in owned:
        s.cleanup()
        assert not s.mpv.is_running()


def test_window_close_and_reopen_restores_real_position(sessions, media):
    s = sessions()
    s.play(media[1], window=True)
    s.seek(12.5)
    process = s.mpv._process
    s.windows[0].close()
    assert s.mpv._process is process and s.mpv.is_running()
    restored = s.play(media[1])
    print(f"window_close: saved=12.5 restored={restored}")
    assert restored == pytest.approx(12.5, abs=.4), f"restored={restored}"


def test_stop_and_reopen_restores_real_position(sessions, media):
    s = sessions()
    s.play(media[1])
    s.seek(18)
    assert s.ctl.stop() is True
    restored = s.play(media[1])
    print(f"stop: saved=18 restored={restored}")
    assert restored == pytest.approx(18, abs=.4), f"restored={restored}"


def test_controller_exit_and_new_process_restore_real_position(sessions, media):
    s = sessions()
    s.play(media[1])
    s.seek(20)
    process = s.mpv._process
    s.ctl.close()
    assert process.poll() is not None
    other = sessions()
    restored = other.play(media[1])
    print(f"controller_exit: saved=20 restored={restored}")
    assert restored == pytest.approx(20, abs=.4), f"restored={restored}"


def test_repeated_stop_and_close_preserve_checkpoint(sessions, media):
    s = sessions()
    s.play(media[1])
    s.seek(24)
    assert s.ctl.stop() is True
    assert s.ctl.stop() is True
    s.ctl.close()
    s.ctl.close()
    assert sessions().play(media[1]) == pytest.approx(24, abs=.4)


def test_a_b_a_uses_separate_native_checkpoints(sessions, media):
    s = sessions()
    s.play(media[1])
    s.seek(14)
    assert s.play(media[2]) == pytest.approx(0, abs=.4)
    s.seek(30)
    assert s.play(media[1]) == pytest.approx(14, abs=.4)
    assert s.ctl.stop() is True
    assert s.play(media[2]) == pytest.approx(30, abs=.4)


def test_natural_eof_does_not_recreate_finished_checkpoint(sessions, media):
    s = sessions()
    s.play(media[1])
    s.seek(12)
    assert s.ctl.stop() is True
    assert s.play(media[1]) == pytest.approx(12, abs=.4)
    s.seek(59.5)
    s.mpv.set_property("pause", False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and s.mpv.get_property("idle-active", timeout=.3) is not True:
        time.sleep(.03)
    assert s.mpv.get_property("idle-active", timeout=.3) is True
    s.ctl.close()
    assert sessions().play(media[1]) == pytest.approx(0, abs=.4)


def test_idle_without_media_does_not_create_checkpoint(sessions):
    s = sessions()
    assert s.mpv.start(taskkill=False)
    assert s.ctl.stop() is True
    s.ctl.close()
    assert not list(s.watch.iterdir())


def test_native_success_without_persisted_file_does_not_confirm_save(sessions, media, monkeypatch):
    s = sessions()
    s.play(media[1])
    s.seek(21)
    not_directory = s.root / "not-directory"
    not_directory.write_text("isolated failure fixture", encoding="utf-8")
    assert s.mpv.set_property("watch-later-directory", str(not_directory))["error"] == "success"
    # MPV acknowledges this command even when its own file write fails.
    assert s.mpv._send_command("write-watch-later-config")["error"] == "success"
    with pytest.raises((RuntimeError, OSError)):
        s.mpv.write_watch_later_config(str(media[1]))
    log = Mock()
    monkeypatch.setattr("app.movie.movie_controller.logger", log)
    process = s.mpv._process
    s.ctl.close()
    assert process.poll() is not None
    assert log.error.called
    log.info.assert_not_called()


@pytest.mark.parametrize("policy", ["ignore-path", "relative-directory"])
def test_native_checkpoint_options_are_read_from_the_player(sessions, media, policy):
    s = sessions()
    s.play(media[1])
    s.seek(26)
    if policy == "ignore-path":
        assert s.mpv.set_property("ignore-path-in-watch-later-config", True)["error"] == "success"
    else:
        relative = os.path.relpath(s.watch)
        assert s.mpv.set_property("watch-later-directory", relative)["error"] == "success"
    assert s.mpv.write_watch_later_config(str(media[1])) is True
    assert s.ctl.stop() is True
    assert s.play(media[1]) == pytest.approx(26, abs=.4)
