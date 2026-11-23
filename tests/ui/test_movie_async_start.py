"""Exercise Qt startup and cancellation with controlled MPV replies."""
import threading

import pytest
from PySide6.QtCore import QTimer

from tests.ui.test_movie_voice_window import scene, wait_for  # noqa: F401


@pytest.mark.parametrize("scene", ["native_embed", "native_external"], indirect=True)
def test_cold_start_keeps_gui_live_and_duplicate_request_uses_one_load(scene):
    s = scene
    entered, release, heartbeat = threading.Event(), threading.Event(), threading.Event()
    gui_thread = threading.get_ident()
    threads = []

    def launch(**kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        s.mpv.is_running.return_value = True
        return True

    s.mpv.start.side_effect = launch
    try:
        assert s.panel._play_item(s.item)
        wait_for(entered.is_set)
        QTimer.singleShot(0, heartbeat.set)
        wait_for(heartbeat.is_set)
        assert threads and threads[0] != gui_thread
        assert s.panel._play_item(s.item)
        assert s.panel.lbl_status.text().startswith("正在加载")
        s.mpv.loadfile.assert_not_called()
    finally:
        release.set()
    wait_for(lambda: s.panel._native_cancel is None)
    s.mpv.start.assert_called_once()
    s.mpv.loadfile.assert_called_once_with(s.item["first_url"])
    assert s.controller.playback_startup().state == "accepted"
    assert s.panel.lbl_status.text().startswith("正在加载")


@pytest.mark.parametrize("scene", ["native_embed"], indirect=True)
def test_replacement_cancels_cold_process_before_any_old_load(scene):
    s = scene
    entered = threading.Event()
    launches = []

    def launch(*, cancel, **kwargs):
        launches.append(cancel)
        if len(launches) == 1:
            entered.set()
            assert cancel.wait(3)
            return False
        s.mpv.is_running.return_value = True
        return True

    s.mpv.start.side_effect = launch
    assert s.panel._play_item(s.item)
    wait_for(entered.is_set)
    replacement = {"first_url": "https://media.example.invalid/new.mp4", "vod_name": "新片"}
    assert s.panel._play_item(replacement)
    wait_for(lambda: s.controller.current_item() == replacement and s.panel._native_cancel is None)
    assert launches[0].is_set()
    assert len(launches) == 2
    s.mpv.loadfile.assert_called_once_with(replacement["first_url"])


@pytest.mark.parametrize("scene", ["native_embed"], indirect=True)
@pytest.mark.parametrize("during", ["pipe", "load_reply"])
def test_window_close_cancels_pending_movie_without_reopening(scene, during):
    s = scene
    entered, release = threading.Event(), threading.Event()
    cancel_seen = []

    def launch(*, cancel, **kwargs):
        cancel_seen.append(cancel)
        if during == "pipe":
            entered.set()
            assert cancel.wait(3)
            return False
        s.mpv.is_running.return_value = True
        return True

    def loadfile(_):
        s.mpv.set_property.assert_called_with("pause", True)
        entered.set()
        assert release.wait(3)
        return {"error": "success"}

    s.mpv.start.side_effect = launch
    s.mpv.loadfile.side_effect = loadfile
    s.mpv.stop_process.side_effect = lambda: setattr(s.mpv.is_running, "return_value", False)
    try:
        assert s.panel._play_item(s.item)
        wait_for(entered.is_set)
        assert s.window.isVisible()
        s.window.close()
        assert not s.window.isVisible()
        assert cancel_seen[0].is_set()
    finally:
        release.set()
    wait_for(lambda: not s.panel._native_start._running)
    assert not s.window.isVisible()
    assert not s.window._playing
    assert s.controller.current_item() is None
    if during == "pipe":
        s.mpv.loadfile.assert_not_called()
    else:
        s.mpv.loadfile.assert_called_once()
        s.mpv.set_property.assert_called_once_with("pause", True)
        s.mpv.stop_process.assert_called_once()


@pytest.mark.parametrize("scene", ["native_external"], indirect=True)
def test_pipe_failure_exposes_explicit_retry_without_claiming_playback(scene):
    s = scene

    def retry(**kwargs):
        s.mpv.is_running.return_value = True
        return True

    s.mpv.start.return_value = False
    s.mpv.start.side_effect = None
    assert s.panel._play_item(s.item)
    wait_for(lambda: not s.panel.btn_retry_startup.isHidden())
    assert s.controller.playback_startup().state == "start_failed"
    assert s.panel.lbl_status.text().startswith("暂时无法播放")
    s.mpv.loadfile.assert_not_called()

    s.mpv.start.side_effect = retry
    s.panel.btn_retry_startup.click()
    wait_for(lambda: s.panel._native_cancel is None and s.mpv.loadfile.call_count == 1)
    assert s.controller.playback_startup().state == "accepted"
