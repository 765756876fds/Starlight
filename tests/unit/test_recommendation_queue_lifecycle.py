"""Offline chart -> actual controller load -> next/EOF. No user data or network."""
import threading

import pytest

from app.core.operation_result import OperationStatus
from app.music.queue import PlayMode
from app.voice.runtime import VoiceRuntime
from tests.unit.test_music_recommendation import controller as controller, song

MODES = [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE]


def prepare(controller, monkeypatch):
    controller._resolver = object()  # Successful direct-ID resolution never uses fallback.
    controller._running = True
    idle = [False]
    get = controller.mpv.get_property
    load = controller.mpv.loadfile
    def read(key, **kwargs):
        return idle[0] if key == "idle-active" else get(key, **kwargs)
    def loaded(url):
        idle[0] = False
        return load(url)
    monkeypatch.setattr(controller.mpv, "get_property", read)
    monkeypatch.setattr(controller.mpv, "loadfile", loaded)
    monkeypatch.setattr(controller.mpv, "is_running", lambda: True, raising=False)
    monkeypatch.setattr(controller.mpv, "stop", lambda: idle.__setitem__(0, True), raising=False)
    done = threading.Event()
    resolve = controller._resolve_online_worker
    def resolved(*args):
        try:
            return resolve(*args)
        finally:
            done.set()
    monkeypatch.setattr(controller, "_resolve_online_worker", resolved)
    return idle, done


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("action", ["manual", "natural", "换一首", "换一个"])
def test_multi_chart_survives_real_next_paths(controller, monkeypatch, mode, action):
    idle, done = prepare(controller, monkeypatch)
    controller.online_manager.tracks = [song(n) for n in range(5)]
    controller.queue.set_play_mode(mode)
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    assert controller.queue.size() == 5 and controller.queue.get_current_index() == 0
    first = controller.get_current_song()["track_id"]
    session = controller.recommendations.session
    reads = controller.online_manager.details
    if action == "manual":
        controller.next()
    elif action == "natural":
        idle[0] = True
        controller._last_idle = False
        controller._auto_next()
    else:
        runtime = VoiceRuntime({"asr": {}, "intent": {}})
        runtime.command_bus = controller.command_bus
        runtime.music_mpv_provider = lambda: controller.mpv
        runtime.music_controller_provider = lambda: controller
        runtime._enqueue_speak = lambda *a, **k: None
        workers = []
        execute = runtime.command_bus.execute_async
        def tracked(*args, **kwargs):
            worker = execute(*args, **kwargs)
            workers.append(worker)
            return worker
        monkeypatch.setattr(runtime.command_bus, "execute_async", tracked)
        runtime._execute_intent(runtime.intent_router.parse(action), action)
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
    assert done.wait(2), "actual online resolver did not finish"
    assert len(controller.mpv.loaded) == 2
    assert controller.queue.size() == 5
    assert controller.recommendations.session is session
    assert controller.online_manager.details == reads
    current = controller.get_current_song()
    assert current["extra"]["_starlight_recommended"] is True
    assert (current["track_id"] == first) is (action == "natural" and mode == PlayMode.REPEAT_ONE)
    assert controller.queue.get_current()["track_id"] == current["track_id"]


@pytest.mark.parametrize("mode", MODES)
def test_single_track_chart_keeps_its_real_entry(controller, monkeypatch, mode):
    idle, done = prepare(controller, monkeypatch)
    controller.online_manager.tracks = [song(0)]
    controller.queue.set_play_mode(mode)
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    assert controller.queue.size() == 1 and controller.queue.get_current_index() == 0
    controller.next()
    if mode == PlayMode.SEQUENTIAL:
        assert controller.get_current_song() is None and idle[0]
        assert len(controller.mpv.loaded) == 1
    else:
        assert done.wait(2)
        assert len(controller.mpv.loaded) == 2
        assert controller.get_current_song()["track_id"] == "0"
    assert controller.queue.size() == 1


def test_empty_chart_never_fakes_started_session(controller):
    controller.online_manager.tracks = []
    assert controller.recommend_music().status is OperationStatus.FAILED
    assert controller.queue.size() == 0 and controller.queue.get_current_index() == -1
    assert not controller.mpv.loaded and controller.recommendations.session is None


def test_direct_single_play_has_no_implicit_chart_queue(controller, monkeypatch, capsys):
    idle, _ = prepare(controller, monkeypatch)
    controller.queue.set_play_mode(PlayMode.SHUFFLE)
    assert controller.play_online(song(0)) is True
    assert controller.queue.size() == 0 and controller.recommendations.session is None
    controller.next()
    assert idle[0] and controller.get_current_song() is None
    diagnostics = capsys.readouterr().out
    assert "music_collection stage=next reason=manual" in diagnostics
    assert "queue_size=0 index=-1 mode=shuffle" in diagnostics


def test_manual_clear_does_not_revive_old_recommendation(controller, monkeypatch):
    prepare(controller, monkeypatch)
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    controller.clear_queue()
    assert controller.queue.size() == 0 and controller.recommendations.session is None
    before = len(controller.mpv.loaded)
    assert controller._cmd_recommend_next(None).status is OperationStatus.NEEDS_PARAMETER
    assert len(controller.mpv.loaded) == before


def test_initial_load_snapshot_is_not_the_committed_collection(controller, monkeypatch):
    controller.online_manager.tracks = [song(n) for n in range(5)]
    before_callback = []
    load = controller.mpv.loadfile
    def inspect(url):
        before_callback.append((controller.queue.size(), controller.queue.get_current_index()))
        return load(url)
    monkeypatch.setattr(controller.mpv, "loadfile", inspect)
    assert controller.recommend_music().status is OperationStatus.CONFIRMED
    assert before_callback == [(0, -1)]
    assert (controller.queue.size(), controller.queue.get_current_index()) == (5, 0)
    assert controller.recommendations.session is not None
