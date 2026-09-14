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
from typing import Any, Callable, Dict, Optional


class ScheduledTask:
    """定时任务"""

    def __init__(self, task_id: str, func: Callable, interval: float,
                 immediate: bool = False, enabled: bool = True):
        self.task_id = task_id
        self.func = func
        self.interval = interval  # 秒
        self.immediate = immediate
        self.enabled = enabled
        self.last_run: Optional[float] = None
        self.next_run: Optional[float] = None
        self.run_count = 0
        self.error_count = 0
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()

    def should_run(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.last_run is None:
            return self.immediate or True
        return now - self.last_run >= self.interval

    def run(self):
        """执行任务"""
        with self._lock:
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

    def status(self) -> Dict[str, Any]:
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
        self._tasks: Dict[str, ScheduledTask] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._tick_interval = tick_interval

    def add_task(self, task_id: str, func: Callable, interval: float,
                 immediate: bool = False) -> ScheduledTask:
        """添加定时任务"""
        task = ScheduledTask(task_id, func, interval, immediate)
        with self._lock:
            self._tasks[task_id] = task
        return task

    def remove_task(self, task_id: str):
        """移除定时任务"""
        with self._lock:
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

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """获取任务"""
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self) -> Dict[str, Dict[str, Any]]:
        """列出所有任务状态"""
        with self._lock:
            return {tid: task.status() for tid, task in self._tasks.items()}

    def start(self):
        """启动调度器"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="scheduler"
        )
        self._thread.start()

    def stop(self):
        """停止调度器"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        """主循环"""
        while self._running:
            now = time.time()

            # 收集需要运行的任务
            with self._lock:
                to_run = [t for t in self._tasks.values() if t.should_run(now)]

            # 执行任务
            for task in to_run:
                task.run()

            time.sleep(self._tick_interval)

    def run_once(self, task_id: str):
        """立即执行一次任务"""
        with self._lock:
            task = self._tasks.get(task_id)
        if task:
            task.run()


# 全局调度器单例
_scheduler: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    """获取全局调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
