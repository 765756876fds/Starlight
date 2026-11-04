"""Movie shutdown checkpoints: ordering, actual IPC evidence and bounded waits."""
import hashlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.movie.movie_controller import MovieController
from app.music.mpv import MpvController


class Player:
    def __init__(self):
        self.running = True
        self.calls = []
        self.save_result = True
        self.stop_result = True

    def is_running(self):
        return self.running

    def write_watch_later_config(self, path, timeout=1.):
        self.calls.append(("save", path, timeout))
        if isinstance(self.save_result, Exception):
            raise self.save_result
        return self.save_result

    def stop(self):
        self.calls.append(("stop",))
        return self.stop_result

    def stop_process(self):
        self.calls.append(("close",))
        self.running = False


def controller(tmp_path):
    c = MovieController.__new__(MovieController)
    c.mpv = Player()
    c._current = {"local_file": True, "first_url": str(tmp_path / "电影 A.mp4")}
    c._current_index = 0
    c._stopping = False
    c._subscribed = False
    c._write_state = Mock()
    return c


def test_local_stop_saves_before_unloading_and_close_does_not_resave(tmp_path):
    c = controller(tmp_path)
    path = c._current["first_url"]
    assert c.stop() is True
    c.stop()
    c.close()
    assert c.mpv.calls == [("save", path, 1.), ("stop",), ("stop",), ("close",)]


def test_local_close_saves_before_process_cleanup_and_is_repeatable(tmp_path):
    c = controller(tmp_path)
    path = c._current["first_url"]
    c.close()
    c.close()
    assert c.mpv.calls == [("save", path, 1.), ("close",), ("close",)]


@pytest.mark.parametrize("result", [TimeoutError("deadline"), RuntimeError("mpv error"), None])
@pytest.mark.parametrize("action", ["stop", "close"])
def test_checkpoint_failure_is_logged_but_does_not_block_shutdown(tmp_path, monkeypatch, result, action):
    c = controller(tmp_path)
    log = Mock()
    monkeypatch.setattr("app.movie.movie_controller.logger", log)
    c.mpv.save_result = result
    getattr(c, action)()
    assert c.mpv.calls[0][0] == "save"
    assert c.mpv.calls[-1][0] == ("stop" if action == "stop" else "close")
    assert log.error.called
    log.info.assert_not_called()


@pytest.mark.parametrize("kind", ["no_item", "online", "not_running"])
def test_no_local_media_does_not_checkpoint(tmp_path, kind):
    c = controller(tmp_path)
    if kind == "no_item":
        c._current = None
    elif kind == "online":
        c._current = {"first_url": "https://example.test/video.mp4"}
    else:
        c.mpv.running = False
    c.close()
    assert not any(call[0] == "save" for call in c.mpv.calls)


def test_failed_stop_keeps_context_and_close_saves_latest_position(tmp_path):
    c = controller(tmp_path)
    c.mpv.stop_result = False
    assert c.stop() is False
    assert c._current is not None
    c.close()
    assert [call[0] for call in c.mpv.calls] == ["save", "stop", "save", "close"]


@pytest.fixture
def ipc(tmp_path):
    media = tmp_path / "中文 空格.mp4"
    media.write_bytes(b"isolated test")
    watch = tmp_path / "watch"
    watch.mkdir()
    mpv = MpvController.__new__(MpvController)
    mpv.video = True
    mpv._pipe_lock = threading.Lock()
    props = {"idle-active": False, "path": str(media), "eof-reached": False,
             "time-pos": 22.5, "watch-later-directory": str(watch),
             "ignore-path-in-watch-later-config": False}
    checkpoint = watch / hashlib.md5(str(media).encode()).hexdigest().upper()

    def send(command, params=None, timeout=3., **kwargs):
        assert kwargs.get("_locked") is True
        assert not mpv._pipe_lock.acquire(blocking=False), "checkpoint RPC batch must hold the pipe lock"
        if command == "get_property":
            return {"error": "success", "data": props[params[0]]}
        if command == "write-watch-later-config":
            checkpoint.write_text("start=22.500000\n", encoding="utf-8")
            return {"error": "success"}
        raise AssertionError(command)

    mpv._send_command = Mock(side_effect=send)
    return mpv, media, props, checkpoint


def test_ipc_success_requires_native_checkpoint_file(ipc):
    mpv, media, _, checkpoint = ipc
    assert mpv.write_watch_later_config(str(media)) is True
    assert checkpoint.read_text() == "start=22.500000\n"


@pytest.mark.parametrize("response", [None, {"error": "error running command"}, {"error": "success"}])
def test_error_timeout_or_missing_file_cannot_confirm_saved(ipc, response):
    mpv, media, props, checkpoint = ipc
    original = mpv._send_command.side_effect
    mpv._send_command.side_effect = lambda cmd, *a, **kw: response if cmd == "write-watch-later-config" else original(cmd, *a, **kw)
    with pytest.raises((TimeoutError, RuntimeError, OSError)):
        mpv.write_watch_later_config(str(media))
    assert not checkpoint.exists()


@pytest.mark.parametrize("change", [{"idle-active": True}, {"eof-reached": True},
                                     {"path": "another.mp4"}, {"time-pos": None}, {"time-pos": 0.}])
def test_idle_eof_wrong_media_or_no_progress_does_not_write(ipc, change):
    mpv, media, props, checkpoint = ipc
    props.update(change)
    assert mpv.write_watch_later_config(str(media)) is False
    assert not checkpoint.exists()
    assert all(c.args[0] != "write-watch-later-config" for c in mpv._send_command.call_args_list)


def test_checkpoint_wait_for_busy_pipe_is_bounded(ipc):
    mpv, media, _, _ = ipc
    mpv._pipe_lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            mpv.write_watch_later_config(str(media), timeout=.02)
        assert time.monotonic() - started < .5
        mpv._send_command.assert_not_called()
    finally:
        mpv._pipe_lock.release()


def test_unresponsive_ipc_read_has_deadline_and_releases_lock(ipc):
    mpv, media, _, _ = ipc
    mpv._pipe = Mock()
    mpv._request_id = 0
    mpv._pipe_available = lambda: 0
    mpv._send_command = MpvController._send_command.__get__(mpv)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="unconfirmed"):
        mpv.write_watch_later_config(str(media), timeout=.03)
    assert time.monotonic() - started < .5
    assert mpv._pipe_lock.acquire(blocking=False)
    mpv._pipe_lock.release()


@pytest.mark.parametrize("action", ["stop", "close"])
def test_unresponsive_checkpoint_does_not_block_shutdown(ipc, tmp_path, monkeypatch, action):
    mpv, media, _, _ = ipc
    c = controller(tmp_path)
    c._current["first_url"] = str(media)
    c.mpv = mpv
    mpv.is_running = lambda: True
    mpv.stop = Mock(return_value=True)
    mpv.stop_process = Mock()
    mpv._pipe = Mock()
    mpv._request_id = 0
    mpv._pipe_available = lambda: 0
    mpv._send_command = MpvController._send_command.__get__(mpv)
    log = Mock()
    monkeypatch.setattr("app.movie.movie_controller.logger", log)
    started = time.monotonic()
    getattr(c, action)()
    elapsed = time.monotonic() - started
    print(f"unresponsive_checkpoint_{action}: elapsed={elapsed:.3f}s")
    assert 1. <= elapsed < 1.5
    (mpv.stop if action == "stop" else mpv.stop_process).assert_called_once_with()
    assert log.error.called
    log.info.assert_not_called()


def test_checkpoint_uses_one_budget_for_the_entire_rpc_batch(ipc, monkeypatch):
    mpv, media, _, _ = ipc
    clock = [100.]
    monkeypatch.setattr("app.music.mpv.time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = mpv._send_command.side_effect

    def slow_command(*args, **kwargs):
        clock[0] += .26
        return original(*args, **kwargs)

    mpv._send_command.side_effect = slow_command
    with pytest.raises(TimeoutError, match="deadline"):
        mpv.write_watch_later_config(str(media), timeout=1.)
    timeouts = [call.kwargs["timeout"] for call in mpv._send_command.call_args_list]
    assert timeouts == pytest.approx([1., .74, .48, .22])
    assert all(call.args[0] != "write-watch-later-config" for call in mpv._send_command.call_args_list)
    assert mpv._pipe_lock.acquire(blocking=False)
    mpv._pipe_lock.release()


@pytest.mark.parametrize("contents", ["", "start=nan\n", "start=0\n", "start=2\n"])
def test_missing_or_stale_position_cannot_confirm_save(ipc, contents):
    mpv, media, _, checkpoint = ipc
    original = mpv._send_command.side_effect

    def write_incorrect_position(command, *args, **kwargs):
        if command == "write-watch-later-config":
            checkpoint.write_text(contents, encoding="utf-8")
            return {"error": "success"}
        return original(command, *args, **kwargs)

    mpv._send_command.side_effect = write_incorrect_position
    with pytest.raises(RuntimeError, match="position"):
        mpv.write_watch_later_config(str(media))


@pytest.mark.parametrize("kind", ["missing", "music"])
def test_missing_file_and_music_instance_never_write(ipc, kind):
    mpv, media, _, _ = ipc
    if kind == "missing":
        media.unlink()  # This fixture's own dummy file only.
    else:
        mpv.video = False
    assert mpv.write_watch_later_config(str(media)) is False
    mpv._send_command.assert_not_called()
