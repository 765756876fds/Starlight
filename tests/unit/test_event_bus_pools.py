import pytest

pytestmark = pytest.mark.unit

# -*- coding: utf-8 -*-
"""EventBus 异步双池隔离测试：状态池可丢 / 临界池不丢 / 路由正确。"""
import threading
import time

from app.core.event_bus import (
    _STATE_POOL_EVENTS,
    Event,
    EventBus,
    EventType,
)


def _make_bus():
    return EventBus()


def test_state_pool_discards_on_congestion():
    """状态池拥堵（pending 达阈值）时，新状态事件被丢弃（合并语义）。"""
    bus = _make_bus()
    got = []
    bus.subscribe(EventType.GAME_TIME_UPDATE, lambda e: got.append(e))
    try:
        # 人为填满状态池 pending 计数
        bus._state_pending = bus._STATE_MAX_PENDING
        bus.publish_async(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 1}))
        time.sleep(0.2)
        assert got == [], "拥堵时状态事件不应被提交执行"
        assert bus._state_pending == bus._STATE_MAX_PENDING
    finally:
        bus._state_pending = 0
        bus.shutdown()


def test_state_pool_executes_when_idle():
    """状态池空闲时，状态事件正常提交执行。"""
    bus = _make_bus()
    got = []
    bus.subscribe(EventType.GAME_TIME_UPDATE, lambda e: got.append(e.get("t")))
    try:
        bus.publish_async(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 7}))
        deadline = time.time() + 2
        while not got and time.time() < deadline:
            time.sleep(0.02)
        assert got == [7]
    finally:
        bus.shutdown()


def test_critical_pool_never_drops_even_when_congested():
    """临界池拥堵时仍提交执行（只记录告警，绝不静默丢弃）。"""
    bus = _make_bus()
    got = []
    bus.subscribe(EventType.CHAMPION_KILL, lambda e: got.append(e.get("k")))
    try:
        # 模拟临界池拥堵（超过告警阈值）
        bus._critical_pending = bus._CRITICAL_MAX_PENDING
        bus.publish_async(Event(
            event_type=EventType.CHAMPION_KILL, data={"k": 3},
        ))
        deadline = time.time() + 2
        while not got and time.time() < deadline:
            time.sleep(0.02)
        assert got == [3], "临界事件在拥堵时也绝不能丢"
    finally:
        bus._critical_pending = 0
        bus.shutdown()


def test_routing_state_vs_critical():
    """路由：状态类事件进状态池（可丢），击杀/系统错误等进临界池（不可丢）。"""
    bus = _make_bus()
    try:
        assert EventType.GAME_TIME_UPDATE in _STATE_POOL_EVENTS
        assert EventType.GAME_SNAPSHOT in _STATE_POOL_EVENTS
        assert EventType.MUSIC_STATE_CHANGED in _STATE_POOL_EVENTS
        assert EventType.CHAMPION_KILL not in _STATE_POOL_EVENTS
        assert EventType.SYSTEM_ERROR not in _STATE_POOL_EVENTS
        assert EventType.ALARM_TRIGGERED not in _STATE_POOL_EVENTS
    finally:
        bus.shutdown()


def test_compat_alias_async_executor():
    """兼容：_async_executor 别名指向临界池（历史调用方/测试断言不破坏）。"""
    bus = _make_bus()
    try:
        assert bus._async_executor is bus._critical_executor
        assert bus._async_executor._max_workers == 4
    finally:
        bus.shutdown()


def test_shutdown_waits_for_both_pools():
    """shutdown 优雅关闭双池：提交的事件都能执行完。"""
    bus = _make_bus()
    got = []
    done = threading.Event()

    def handler(e):
        got.append(e.get("n"))
        done.set()

    bus.subscribe("test.both", handler)
    try:
        bus.publish_async(Event(event_type="test.both", data={"n": 1}))
        bus.shutdown()
        assert done.wait(0.5), "shutdown 后事件未执行完"
        assert got == [1]
    finally:
        bus.shutdown()


# 供测试访问的模块级集合（已在顶部 import，此处不再重复）
