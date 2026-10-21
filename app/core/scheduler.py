"""
任务调度器 - 管理定时任务和后台轮询

用于：
- 2999 数据轮询
- LCU 状态轮询
- GameAlarm 倒计时检查
- 音乐状态同步
- 定期清理
"""
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any


class ScheduledTask:
    """定时任务"""

    def __init__(self, task_id: str, func: Callable, interval: float,
                 immediate: bool = False, enabled: bool = True,
                 run_async: bool = False):
        self.task_id = task_id
        self.func = func
        self.interval = interval  # 秒
        self.immediate = immediate
        self.enabled = enabled
        self.run_async = run_async  # 耗时任务：提交线程池执行，不阻塞主循环
        self.last_run: float | None = None
        self.next_run: float | None = None
        self.run_count = 0
        self.error_count = 0
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._running = False  # 防重入：同一任务不允许并发执行

    def should_run(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.last_run is None:
            if self.next_run is not None:
                return now >= self.next_run
            return self.immediate or True
        return now - self.last_run >= self.interval

    def run(self):
        """执行任务（防重入：同一任务已在运行时直接跳过）"""
        with self._lock:
            if self._running:
                return
            self._running = True
            now = time.time()
            self.last_run = now
            self.next_run = now + self.interval
            self.run_count += 1

        try:
            self.func()
        except Exception as e:
            with self._lock:
                self.error_count += 1
                self.last_error = str(e)
            print(f"[Scheduler] 任务 {self.task_id} 异常: {e}")
        finally:
            with self._lock:
                self._running = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "task_id": self.task_id,
                "enabled": self.enabled,
                "interval": self.interval,
                "run_count": self.run_count,
                "error_count": self.error_count,
                "last_run": self.last_run,
                "next_run": self.next_run,
                "last_error": self.last_error,
            }


class Scheduler:
    """
    后台任务调度器

    单线程轮询所有任务，避免创建大量线程。
    任务应该尽量短，长时间任务应该自己开线程。
    """

    def __init__(self, tick_interval: float = 0.1):
        self._tasks: dict[str, ScheduledTask] = {}
        self._oneshot_cancelled: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._tick_interval = tick_interval
        self._stop_event = threading.Event()  # stop() 置位以立即唤醒主循环
        # run_async 任务的有界线程池（进程退出时由解释器 atexit 收尾）
        self._async_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="sched-task"
        )

    def add_task(self, task_id: str, func: Callable, interval: float,
                 immediate: bool = False, run_async: bool = False) -> ScheduledTask:
        """添加定时任务"""
        task = ScheduledTask(task_id, func, interval, immediate, run_async=run_async)
        with self._lock:
            self._tasks[task_id] = task
        return task

    def remove_task(self, task_id: str):
        """移除定时任务"""
        with self._lock:
            self._tasks.pop(task_id, None)

    def schedule_once(self, delay_seconds: float, func: Callable,
                      task_id: str | None = None) -> str:
        """一次性任务：delay_seconds 秒后执行一次，然后自动移除。"""
        import uuid as _uuid
        tid = task_id or ("once-" + _uuid.uuid4().hex[:8])
        due_at = time.time() + max(0.0, delay_seconds)

        def _runner():
            if self._oneshot_cancelled.pop(tid, False):
                return
            try:
                func()
            finally:
                with self._lock:
                    self._tasks.pop(tid, None)

        # 用一个极短 interval 的任务承载，通过 last_run 控制首次触发时机
        task = ScheduledTask(tid, _runner, interval=0.05, immediate=False)
        task.next_run = due_at
        with self._lock:
            self._tasks[tid] = task
            self._oneshot_cancelled[tid] = False
        # 启动调度器（如果未启动）
        if not self._running:
            self.start()
        return tid

    def cancel_once(self, task_id: str):
        """取消一次性任务。"""
        with self._lock:
            self._oneshot_cancelled[task_id] = True
            self._tasks.pop(task_id, None)

    def enable_task(self, task_id: str):
        """启用任务"""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].enabled = True

    def disable_task(self, task_id: str):
        """禁用任务"""
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].enabled = False

    def get_task(self, task_id: str) -> ScheduledTask | None:
        """获取任务"""
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self) -> dict[str, dict[str, Any]]:
        """列出所有任务状态"""
        with self._lock:
            return {tid: task.status() for tid, task in self._tasks.items()}

    def start(self):
        """启动调度器（旧线程未退出时拒绝重启，杜绝双 _loop）"""
        with self._lock:
            if self._running:
                return
            if self._thread is not None and self._thread.is_alive():
                print("[Scheduler] 上一个调度线程仍在运行，拒绝重复启动")
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="scheduler"
            )
            self._thread.start()

    def stop(self):
        """停止调度器（立即唤醒主循环，等待当前任务完成，最多 5 秒）"""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        """主循环（run_async 任务提交线程池执行，不阻塞主循环）"""
        while self._running:
            now = time.time()

            # 收集需要运行的任务
            with self._lock:
                to_run = [t for t in self._tasks.values() if t.should_run(now)]

            # 执行任务
            for task in to_run:
                if task.run_async:
                    try:
                        self._async_executor.submit(task.run)
                    except RuntimeError:
                        task.run()  # 池关闭竞态兜底：降级同步执行，不丢任务
                else:
                    task.run()

            self._stop_event.wait(self._tick_interval)

    def run_once(self, task_id: str):
        """立即执行一次任务"""
        with self._lock:
            task = self._tasks.get(task_id)
        if task:
            task.run()


# 全局调度器单例
_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    """获取全局调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
