"""Offline contention, passive event ordering, fresh input and bounded source work."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.event_bus import Event, EventBus, EventType
from app.music.recommendation import RecommendationService, SourceReadBusy
from app.voice.intent import IntentRouter
from app.voice.observations import VoiceObservations
from app.voice.runtime import VoiceRuntime


def test_slow_observer_does_not_hold_next_input_and_waiting_pairs_are_bounded():
    entered, release = threading.Event(), threading.Event()
    seen = []
    def observe(event):
        seen.append((event.event_type, event.data["number"]))
        if len(seen) == 1:
            entered.set()
            assert release.wait(2)
    observer = VoiceObservations(observe)
    def pair(number):
        return [Event(kind, {"number": number}) for kind in
                (EventType.VOICE_TEXT_RECEIVED, EventType.VOICE_INTENT_PARSED)]
    observer.submit(pair(0))
    try:
        assert entered.wait(1)
        worker = observer._worker
        for number in range(1, 30):
            observer.submit(pair(number))
            assert observer._worker is worker
        assert len(observer._pending[1]) == 2
        release.set()
        worker.join(1)
        assert not worker.is_alive()
        assert seen == [(kind, number) for number in (0, 29) for kind in
                        (EventType.VOICE_TEXT_RECEIVED, EventType.VOICE_INTENT_PARSED)]
    finally:
        release.set()
        observer.close()


def test_close_is_nonblocking_and_cannot_publish_pending_observations():
    entered, release = threading.Event(), threading.Event()
    seen = []
    def observe(event):
        seen.append(event.event_type)
        entered.set()
        release.wait(2)
    observer = VoiceObservations(observe)
    observer.submit([Event("first"), Event("second")])
    assert entered.wait(1)
    worker = observer._worker
    observer.submit([Event("obsolete")])
    try:
        start = time.monotonic()
        observer.close()
        assert time.monotonic() - start < .2
        observer.submit([Event("closed")])
    finally:
        release.set()
        worker.join(1)
    assert not worker.is_alive() and seen == ["first"]


def test_real_speech_dispatch_is_not_blocked_by_passive_observer():
    rt = VoiceRuntime({})
    entered, release = threading.Event(), threading.Event()
    rt.event_bus = EventBus()
    def slow(event):
        entered.set()
        release.wait(2)
    rt.event_bus.subscribe(EventType.VOICE_TEXT_RECEIVED, slow)
    rt.asr = Mock(transcribe=Mock(side_effect=["换个榜单", "停一下"]))
    rt._execute_intent = Mock()
    try:
        rt._process_speech(b"first", 16000)
        assert entered.wait(1)
        rt._process_speech(b"second", 16000)
        assert rt._execute_intent.call_count == 2
        assert rt._execute_intent.call_args.args[0].params == {"action": "pause"}
    finally:
        worker = rt._observations._worker
        rt._observations.close()
        release.set()
        if worker:
            worker.join(1)
        rt.event_bus.shutdown()


def test_elapsed_input_is_not_executed_after_delayed_notification(monkeypatch, caplog):
    rt = VoiceRuntime({})
    rt.asr = Mock(transcribe=Mock(return_value="换个榜单"))
    rt._execute_intent = Mock()
    clock = [100.0]
    monkeypatch.setattr("app.voice.runtime.time.monotonic", lambda: clock[0])
    center = Mock()
    def delayed(*args, **kwargs):
        clock[0] += 9
    center.update.side_effect = delayed
    monkeypatch.setattr("app.voice.runtime.get_notification_center", lambda: center)
    with caplog.at_level("INFO"):
        rt._process_speech(b"old", 16000)
    rt._observations.close()
    rt._execute_intent.assert_not_called()
    assert "等待过久" in center.update.call_args.kwargs["title"]
    assert "notification=9000ms" in caplog.text
    assert "parse=" in caplog.text and "lock_wait_ms=" in caplog.text


def test_router_reports_lock_wait_separately_from_parse():
    router = IntentRouter({})
    entered, result = threading.Event(), []
    def parse():
        entered.set()
        router.parse("停一下")
        result.append(router.get_parse_timing())
    with router._context_lock:
        thread = threading.Thread(target=parse)
        thread.start()
        assert entered.wait(1)
        time.sleep(.04)
    thread.join(1)
    assert not thread.is_alive()
    assert result[0]["lock_wait_ms"] >= 25
    assert result[0]["parse_ms"] < 1000


def test_timeout_and_cancel_cannot_accumulate_source_reads_and_can_recover():
    release, both_started = threading.Event(), threading.Event()
    count = []
    def slow():
        count.append(1)
        if len(count) == 2:
            both_started.set()
        assert release.wait(2)
        return "late"
    service = RecommendationService(None, None)
    service.READ_TIMEOUT = .02
    with ThreadPoolExecutor(max_workers=4) as executor:
        manager = SimpleNamespace(_executor=executor)
        try:
            for _ in range(2):
                with pytest.raises(TimeoutError):
                    service._read(manager, slow, lambda: True)
            assert both_started.wait(1)
            for _ in range(20):
                with pytest.raises(SourceReadBusy):
                    service._read(manager, slow, lambda: True)
            assert len(count) == 2 and len(service._source_futures) == 2
            with pytest.raises(TimeoutError):
                service._read(manager, lambda: pytest.fail("obsolete work submitted"), lambda: False)
        finally:
            release.set()
        for future in service._source_futures:
            assert future.result(1) == "late"
        assert service._read(manager, lambda: "fresh", lambda: True) == "fresh"


def test_restart_reuses_blocked_observer_and_skips_old_sessions():
    entered, release = threading.Event(), threading.Event()
    seen = []
    def observe(event):
        seen.append(event.event_type)
        if event.event_type == "old-text":
            entered.set()
            release.wait(2)
    observer = VoiceObservations(observe)
    observer.submit([Event("old-text"), Event("old-intent")])
    assert entered.wait(1)
    worker = observer._worker
    try:
        for _ in range(5):
            observer.close()
            observer.reopen()
            observer.submit([Event("new-text"), Event("new-intent")])
            assert observer._worker is worker
    finally:
        release.set()
        worker.join(1)
        observer.close()
    assert not worker.is_alive()
    assert seen == ["old-text", "new-text", "new-intent"]
