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
    """状态池按 key 合并：同 key 新事件替换旧事件（保留最新状态，不重复执行）。"""
    bus = _make_bus()
    got = []
    bus.subscribe(EventType.GAME_TIME_UPDATE, lambda e: got.append(e.get("t")))
    try:
        # 队列中已有同 key 待处理事件 → 新事件替换，不追加
        bus._state_queue.append(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 1}))
        bus.publish_async(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 2}))
        deadline = time.time() + 2
        while time.time() < deadline:
            time.sleep(0.02)
        # 只执行最新值；旧事件被替换不重复执行
        assert got == [2], "同 key 合并后只执行最新事件"
    finally:
        bus.shutdown()


def test_state_pool_discards_oldest_when_full():
    """状态池满（64）且无同 key 可合并时，丢弃最早的事件腾出空间（而非丢新）。"""
    bus = _make_bus()
    got = []
    bus.subscribe(EventType.GAME_TIME_UPDATE, lambda e: got.append(e.get("n")))
    try:
        # 填满状态队列（64 个不同 entity_key 的待处理状态事件）
        for i in range(bus._STATE_MAX_PENDING):
            bus._state_queue.append(Event(
                event_type=EventType.GAME_TIME_UPDATE,
                data={"entity_key": f"k{i}", "n": i},
            ))
        # 发新 key 状态事件：无同 key 可合并，应丢弃最旧（n=0）并接收新事件
        bus.publish_async(Event(
            event_type=EventType.GAME_TIME_UPDATE,
            data={"entity_key": "new", "n": 999},
        ))
        deadline = time.time() + 2
        while time.time() < deadline:
            time.sleep(0.02)
        remaining = [e.get("n") for e in bus._state_queue]
        assert 0 not in remaining, "最旧事件应被丢弃（popleft）"
        assert 999 in remaining or 999 in got, "新事件应进入队列并最终执行"
        assert len(bus._state_queue) <= bus._STATE_MAX_PENDING
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
    """运行中 key 的新状态被缓存，执行完后立即消费，最终收敛到最新状态。"""
    bus = _make_bus()
    got = []

    def handler(e):
        got.append(e.get("t"))
        time.sleep(0.15)  # 制造执行窗口，暴露运行中竞态

    bus.subscribe(EventType.GAME_TIME_UPDATE, handler)
    try:
        bus.publish_async(Event(event_type=EventType.GAME_TIME_UPDATE, data={"t": 1}))
        # 等 t=1 真正开始执行（running key 已登记，确定竞态窗口）
        deadline = time.time() + 2
        while EventType.GAME_TIME_UPDATE not in bus._running_state_keys and time.time() < deadline:
            time.sleep(0.01)
        assert EventType.GAME_TIME_UPDATE in bus._running_state_keys
        # 执行期间同 key 到达 2 次：中间值 2 应被覆盖，最终收敛到 3
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
