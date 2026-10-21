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


def test_state_pool_merges_by_key():
    """状态池真合并：每 key 最新值槽——预置待处理值被新事件覆盖，
    单 dispatcher 只执行最新值（旧值从不排队执行）。"""
    bus = _make_bus()
    got = []
    bus.subscribe(EventType.GAME_TIME_UPDATE, lambda e: got.append(e.get("t")))
    try:
        # 预置槽位待处理值（同 key 旧状态），再发布新值覆盖
        bus._state_latest_slots[EventType.GAME_TIME_UPDATE] = Event(
            event_type=EventType.GAME_TIME_UPDATE, data={"t": 1})
        bus.publish_async(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 2}))
        deadline = time.time() + 2
        while time.time() < deadline:
            time.sleep(0.02)
        # 只执行最新值；旧值（t=1）被槽覆盖，从不执行
        assert got == [2], f"同 key 真合并后只执行最新事件，got={got}"
    finally:
        bus.shutdown()


def test_state_pool_slots_keep_latest_per_key():
    """槽位式真合并：每 key 独立保留最新值；同 key 覆盖，异 key 共存（无丢语义）。"""
    bus = _make_bus()
    got = []
    bus.subscribe(EventType.GAME_TIME_UPDATE, lambda e: got.append(e.get("n")))
    try:
        bus._state_latest_slots["k0"] = Event(
            event_type=EventType.GAME_TIME_UPDATE, data={"entity_key": "k0", "n": 0})
        bus._state_latest_slots["k1"] = Event(
            event_type=EventType.GAME_TIME_UPDATE, data={"entity_key": "k1", "n": 1})
        bus._state_latest_slots["k0"] = Event(
            event_type=EventType.GAME_TIME_UPDATE, data={"entity_key": "k0", "n": 100})
        assert bus._state_latest_slots["k0"].data["n"] == 100, "同 key 覆盖为最新值"
        assert len(bus._state_latest_slots) == 2, "异 key 各自保留最新值"
        # 发布触发消费：两 key 最新值（100、1）都会执行；k0 旧值 0 永不执行
        bus.publish_async(Event(
            event_type=EventType.GAME_TIME_UPDATE, data={"entity_key": "k0", "n": 100}))
        deadline = time.time() + 2
        while len(set(got)) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert 0 not in got, f"旧值 0 不应执行（真合并），got={got}"
        assert 100 in got and 1 in got, f"每个 key 最新值应执行，got={got}"
    finally:
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
    """兼容：_async_executor 别名指向临界池（历史调用方/测试断言不破坏）。
    关键池单 worker（max_workers=1）保证严格 FIFO 顺序。"""
    bus = _make_bus()
    try:
        assert bus._async_executor is bus._critical_executor
        assert bus._async_executor._max_workers == 1
    finally:
        bus.shutdown()


def test_state_pool_converges_to_latest_while_running():
    """执行期间同 key 新值写回槽：单 dispatcher 循环消费，最终收敛到最新值。"""
    bus = _make_bus()
    got = []

    def handler(e):
        got.append(e.get("t"))
        time.sleep(0.15)  # 制造执行窗口，暴露执行期间到达的合并竞态

    bus.subscribe(EventType.GAME_TIME_UPDATE, handler)
    try:
        bus.publish_async(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 1}))
        # 等 t=1 真正开始执行（got 有 1 个元素）
        deadline = time.time() + 2
        while not got and time.time() < deadline:
            time.sleep(0.01)
        assert got == [1]
        # 执行期间同 key 到达 2 次：中间值 2 被槽覆盖，最终只播 3
        bus.publish_async(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 2}))
        bus.publish_async(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 3}))
        deadline = time.time() + 3
        while len(got) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert got == [1, 3], f"最终应收敛到最新状态（1 播报后只播 3），got={got}"
    finally:
        bus.shutdown(timeout=0.5)


def test_shutdown_waits_for_both_pools():
    """shutdown 有界等待：已开始执行的任务能完成；未开始的允许被取消（不阻塞不抛异常）。"""
    bus = _make_bus()
    got = []
    done = threading.Event()

    def handler(e):
        got.append(e.get("n"))
        done.set()

    bus.subscribe("test.both", handler)
    try:
        bus.publish_async(Event(event_type="test.both", data={"n": 1}))
        time.sleep(0.15)  # 确保 worker 已取走任务开始执行（不被 cancel）
        t0 = time.monotonic()
        bus.shutdown(timeout=1.0)
        assert time.monotonic() - t0 < 3, "shutdown 应有界返回，不无限阻塞"
        assert done.wait(0.5), "已开始执行的事件应能完成"
        assert got == [1]
    finally:
        bus.shutdown(timeout=0.5)


# 供测试访问的模块级集合（已在顶部 import，此处不再重复）
