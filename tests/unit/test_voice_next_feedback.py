"""Real command/controller/queue, offline sources and explicit MPV evidence."""

import threading
import time
from unittest.mock import Mock

import pytest

from app.core import notifications as notices
from app.core.command_bus import Command, CommandType as C
from app.core.notifications import NotificationCenter, operation_scope
from app.core.operation_result import OperationResult, OperationStatus as S
from app.music.queue import PlayMode
from app.voice.runtime import VoiceRuntime
from tests.unit.test_music_recommendation import controller as controller  # noqa: F401


def wait(predicate, timeout=1.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "bounded feedback wait expired"


@pytest.fixture
def setup(controller, monkeypatch):
    c = controller
    center = NotificationCenter()
    monkeypatch.setattr(notices, "_center", center)
    assert c.recommend_music().status is S.CONFIRMED
    c._resolver = Mock(resolve=Mock(return_value=None))
    c.history_recorder.record_song = Mock()
    original = c.mpv.get_property
    progress = {"on": True, "same_entry": False}

    def prop(name, **kwargs):
        if name == "playlist":
            return [{"id": 1 if progress["same_entry"] else len(c.mpv.loaded),
                     "current": True, "filename": c.mpv.loaded[-1]}]
        if name == "time-pos":
            return time.monotonic() if progress["on"] else 0.0
        return original(name, **kwargs)

    c.mpv.get_property = prop
    c._next_feedback.interval = 0.01
    c._next_feedback.budget = 0.4
    return c, center, progress


def invoke(c, center, action=C.MUSIC_NEXT, params=None):
    operation = center.post("正在切歌", source="voice")
    with operation_scope(operation):
        value = c.command_bus.execute(Command(action, params or {}, source="voice"))
    return operation, value


@pytest.mark.parametrize("text", ["换一首", "下一首"])
def test_runtime_voice_returns_confirmed_on_same_operation(setup, text):
    c, center, _ = setup
    runtime = VoiceRuntime({})
    runtime.command_bus = c.command_bus
    runtime._enqueue_speak = Mock()
    parsed = runtime.intent_router.parse(text)
    assert parsed.intent_type == C.MUSIC_NEXT
    runtime._execute_intent(parsed, text)
    wait(lambda: center.history()[0].status == "success")
    record, = center.history()
    assert record.title == "已确认下一首开始播放"
    assert "尚未收到完成确认" not in record.message
    assert len(c.mpv.loaded) == 2
    runtime._enqueue_speak.assert_not_called()


@pytest.mark.parametrize("mode", [PlayMode.SEQUENTIAL, PlayMode.SHUFFLE, PlayMode.REPEAT_ONE])
def test_mode_queue_preserved_and_load_ack_is_not_start(setup, mode):
    c, center, progress = setup
    c.queue.set_play_mode(mode)
    progress["on"] = False
    operation, result = invoke(c, center)
    assert isinstance(result, OperationResult) and result.status is S.ACCEPTED
    wait(lambda: "已接受" in center.get(operation).title)
    assert center.get(operation).status == "processing"
    assert c.queue.size() == 10 and c.queue.get_play_mode() == mode
    progress["on"] = True
    wait(lambda: center.get(operation).status == "success")
    assert len(center.history()) == 1


def test_natural_next_uses_same_adapter_without_outer_result_downgrade(setup, tmp_path):
    from app.voice.interaction_memory import InteractionMemory
    c, center, _ = setup
    c._running = True
    c.mpv.is_running = lambda: True
    runtime = VoiceRuntime({})
    runtime.command_bus = c.command_bus
    runtime.music_controller_provider = lambda: c
    runtime.music_mpv_provider = lambda: c.mpv
    natural = runtime.configure_natural_media(InteractionMemory(tmp_path / "habits.db"))
    try:
        runtime._execute_intent(runtime.intent_router.parse("这首一般般"), "这首一般般")
        wait(lambda: center.history()[0].status == "success")
        record, = center.history()
        assert record.title == "已确认下一首开始播放"
        assert natural.memory.flush()
        assert len(natural.memory.weak_profile("music")) == 1
    finally:
        assert natural.close()


@pytest.mark.parametrize("empty", [True, False])
def test_empty_and_end_are_explicit(setup, empty):
    c, center, _ = setup
    if empty:
        c.queue.clear()
    else:
        c.queue.set_play_mode(PlayMode.SEQUENTIAL)
        while c.queue.get_current_index() < c.queue.size() - 1:
            c.queue.next()
    operation, result = invoke(c, center)
    assert result.status is S.UNAVAILABLE
    assert ("队列为空" if empty else "队列末尾") in center.get(operation).title
    assert center.get(operation).status == "warning"
    assert len(c.mpv.loaded) == 1


def test_debounce_is_not_failure_or_second_selection(setup):
    c, center, _ = setup
    c._last_next_ts = time.time()
    operation, result = invoke(c, center)
    assert result.status is S.UNAVAILABLE
    assert "重复请求已忽略" in center.get(operation).title
    assert len(c.mpv.loaded) == 1


def test_async_resolution_waits_then_confirms_without_reselecting_chart(setup):
    c, center, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = c.online_manager.get_play_url
    charts = c.online_manager.details

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(1)
        return original(*args, **kwargs)

    c.online_manager.get_play_url = slow
    try:
        operation, result = invoke(c, center)
        assert entered.wait(1) and result.status is S.ACCEPTED
        assert "解析并加载" in center.get(operation).title
        assert center.get(operation).status == "processing"
    finally:
        release.set()
    wait(lambda: center.get(operation).status == "success")
    assert c.online_manager.details == charts


@pytest.mark.parametrize("response", [None, {"error": "loading failed"}])
def test_mpv_unknown_and_explicit_failure_do_not_confirm(setup, response):
    c, center, _ = setup
    c.mpv.response = response
    c._schedule_skip_online = Mock()
    operation, _ = invoke(c, center)
    wait(lambda: center.get(operation).phase == "done")
    record = center.get(operation)
    assert record.status == ("warning" if response is None else "failed")
    assert "已确认" not in record.title


def test_url_failure_is_explicit_and_not_a_normal_empty_result(setup):
    c, center, _ = setup
    c._schedule_skip_online = Mock()
    c.online_manager.get_play_url = Mock(side_effect=TimeoutError())
    c._resolver = Mock(resolve=Mock(return_value=None))
    operation, _ = invoke(c, center)
    wait(lambda: center.get(operation).status == "failed")
    assert "地址解析失败" in center.get(operation).title
    assert len(c.mpv.loaded) == 1


def test_timeout_and_unconfirmed_start_are_bounded(setup):
    c, center, progress = setup
    progress["on"] = False
    operation, _ = invoke(c, center)
    wait(lambda: center.get(operation).phase == "done")
    assert center.get(operation).status == "warning"
    assert "尚未确认起播" in center.get(operation).title


def test_old_entry_even_with_advancing_time_never_confirms(setup):
    c, center, progress = setup
    progress["same_entry"] = True
    operation, _ = invoke(c, center)
    wait(lambda: center.get(operation).phase == "done")
    assert center.get(operation).status == "warning"


def test_new_voice_and_cleared_history_never_receive_old_completion(setup):
    c, center, progress = setup
    progress["on"] = False
    first, _ = invoke(c, center)
    second = center.post("电影已暂停", status="success", source="voice", phase="done")
    progress["on"] = True
    wait(lambda: center.get(first).phase == "done")
    assert center.get(first).status == "warning"
    assert center.get(second).title == "电影已暂停"
    center.clear()
    c._next_feedback.report("loadfile", "confirmed", c._play_request_id)
    assert center.history() == ()


def test_busy_stale_unavailable_and_close(setup):
    c, center, _ = setup
    with c._lock:
        operation, value = invoke(c, center)
    assert value.status is S.UNAVAILABLE and "稍后" in center.get(operation).title
    c._mpv_available = False
    _, value = invoke(c, center)
    assert value.status is S.UNAVAILABLE
    result = c._next_feedback.perform(Command(C.MUSIC_NEXT, is_current=lambda: False))
    assert result.status is S.UNKNOWN
    c._next_feedback.close()
    assert c._next_feedback._thread is None or not c._next_feedback._thread.is_alive()


def test_legacy_manual_next_keeps_none_contract(setup):
    c, center, _ = setup
    result = c.command_bus.execute(Command(C.MUSIC_NEXT, source="ui"))
    assert result is None
    assert center.history() == ()


def test_resolver_timeout_does_not_create_an_unbounded_observer(setup):
    c, center, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = c.online_manager.get_play_url
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(1.2)
        return original(*args, **kwargs)
    c.online_manager.get_play_url = slow
    try:
        operation, _ = invoke(c, center)
        assert entered.wait(1)
        wait(lambda: center.get(operation).phase == "done")
        assert "超时" in center.get(operation).title
        assert len([t for t in threading.enumerate() if t.name == "music-next-feedback"]) <= 1
        c._play_request_id += 1  # existing controller epoch invalidates the delayed URL
    finally:
        release.set()
    wait(lambda: c._next_feedback._thread is None)
    assert len(c.mpv.loaded) == 1


def test_late_old_resolver_does_not_load_or_overwrite_new_next(setup):
    c, center, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = c.online_manager.get_play_url
    first_call = [True]
    def delayed(*args, **kwargs):
        if first_call[0]:
            first_call[0] = False
            entered.set()
            assert release.wait(1.5)
        return original(*args, **kwargs)
    c.online_manager.get_play_url = delayed
    try:
        first, _ = invoke(c, center)
        assert entered.wait(1)
        c._last_next_ts = 0  # Two distinct commands outside the tested debounce window.
        second, _ = invoke(c, center)
        wait(lambda: center.get(second).status == "success")
        path = c.mpv.loaded[-1]
    finally:
        release.set()
    wait(lambda: center.get(first).phase == "done")
    assert len(c.mpv.loaded) == 2 and c.mpv.loaded[-1] == path
    assert center.get(first).status == "warning"
    assert center.get(second).status == "success"
    assert len(center.history()) == 2


def test_late_ipc_success_sample_is_rechecked_before_confirmation(setup):
    c, center, _ = setup
    observer = c._next_feedback
    old = observer._property
    entered, release = threading.Event(), threading.Event()
    def delayed(name):
        result = old(name)
        if name == "time-pos":
            entered.set()
            assert release.wait(1)
        return result
    observer._property = delayed
    try:
        operation, _ = invoke(c, center)
        assert entered.wait(1)
        c._play_request_id += 1
    finally:
        release.set()
    wait(lambda: center.get(operation).phase == "done")
    assert center.get(operation).status == "warning"
    assert "过期" in center.get(operation).title


def test_superseded_before_handler_starts_closes_original_notification(setup):
    c, center, _ = setup
    c.command_bus.register(C.MUSIC_VOLUME_UP, lambda command: True)
    runtime = VoiceRuntime({})
    runtime.command_bus = c.command_bus
    execute = c.command_bus.execute
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def gated(command):
        if command.command_type == C.MUSIC_NEXT:
            entered.set()
            assert release.wait(1)
            try:
                return execute(command)
            finally:
                finished.set()
        return execute(command)
    c.command_bus.execute = gated
    try:
        runtime._execute_intent(runtime.intent_router.parse("下一首"), "下一首")
        assert entered.wait(1)
        first = center.history()[0].id
        runtime._execute_intent(runtime.intent_router.parse("音乐声音大一点"), "音乐声音大一点")
        second = center.history()[0].id
        assert second != first
    finally:
        release.set()
    assert finished.wait(1)
    wait(lambda: center.get(first).phase == "done")
    assert center.get(first).status == "warning"
    assert len(c.mpv.loaded) == 1 and len(center.history()) == 2
