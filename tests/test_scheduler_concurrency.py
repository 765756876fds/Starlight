# -*- coding: utf-8 -*-
"""Scheduler 并发安全测试：防双循环 / 防重入 / run_async 耗时隔离。"""
import threading
import time

import pytest

from app.core.scheduler import Scheduler


def _scheduler_threads():
    return [t for t in threading.enumerate() if t.name == "scheduler"]


# ---------- ① 防双循环 ----------

def test_start_refuses_when_old_thread_alive():
    """旧 _loop 线程仍存活时，start() 必须拒绝，绝不能双 _loop。"""
    started = threading.Event()
    release = threading.Event()
    called = []

    def slow():
        called.append(1)
        started.set()
        release.wait(3)

    s = Scheduler(tick_interval=0.02)
    s.add_task("slow", slow, interval=1.0, immediate=True)
    try:
        s.start()
        assert started.wait(1), "慢任务未启动"
        # 模拟 stop 已置 _running=False，但线程仍在跑慢任务（未真正退出）
        s._running = False
        s.start()  # 应当被 is_alive() 检查拒绝
        time.sleep(0.15)
        assert len(_scheduler_threads()) == 1, "出现了第二个 _loop 线程"
    finally:
        release.set()
        s.stop()


def test_stop_then_start_normal():
    """stop() 正常等线程退出后，再 start() 应能正常重启（仅单线程）。"""
    s = Scheduler(tick_interval=0.02)
    s.add_task("t", lambda: None, interval=0.05, immediate=True)
    s.start()
    time.sleep(0.05)
    s.stop()
    assert len(_scheduler_threads()) == 0
    s.start()
    time.sleep(0.05)
    assert len(_scheduler_threads()) == 1
    s.stop()


# ---------- ② 防重入 ----------

def test_run_once_skips_when_task_running():
    """主循环正在执行任务时，另一线程 run_once 不得并发执行。"""
    in_flight = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append("begin")
        in_flight.set()
        release.wait(1)
        calls.append("done")

    s = Scheduler(tick_interval=0.02)
    s.add_task("t", slow, interval=10, immediate=True)
    try:
        s.start()
        assert in_flight.wait(1), "主循环未开始执行任务"
        s.run_once("t")  # 应被防重入锁跳过
        time.sleep(0.15)
        # begin 只应出现一次（主循环）；run_once 并发执行会再加一次
        assert calls.count("begin") == 1, f"任务被并发执行: {calls}"
    finally:
        release.set()
        s.stop()


def test_reentry_flag_reset_after_finish():
    """任务执行完成后 _running 复位，后续 run_once 可再次执行。"""
    s = Scheduler(tick_interval=0.02)
    counter = []

    def fast():
        counter.append(1)

    s.add_task("t", fast, interval=10, immediate=False)
    try:
        s.run_once("t")
        s.run_once("t")
        assert len(counter) == 2
        # 直接构造：模拟重入锁的完整生命周期
        task = s.get_task("t")
        assert task._running is False
    finally:
        s.stop()


# ---------- ③ run_async 耗时隔离 ----------

def test_async_task_does_not_block_loop():
    """run_async 任务提交线程池，慢任务阻塞期间其他任务照常轮询。"""
    blocker = threading.Event()
    release = threading.Event()
    tick_count = []

    def slow_async():
        blocker.set()
        release.wait(2)

    def ticker():
        tick_count.append(time.time())

    s = Scheduler(tick_interval=0.02)
    s.add_task("slow", slow_async, interval=1.0, immediate=True, run_async=True)
    s.add_task("tick", ticker, interval=0.05, immediate=True)
    try:
        s.start()
        assert blocker.wait(1), "慢任务未进入线程池"
        time.sleep(0.25)  # 慢任务仍在阻塞
        assert len(tick_count) >= 3, f"主循环被慢任务阻塞: {len(tick_count)} 次"
    finally:
        release.set()
        s.stop()


def test_sync_task_default_not_async():
    """默认任务（未标记 run_async）走同步路径（兼容现状）。"""
    s = Scheduler(tick_interval=0.02)
    task = s.add_task("t", lambda: None, interval=1.0)
    assert task.run_async is False
    s.stop()
