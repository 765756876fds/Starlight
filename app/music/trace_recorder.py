"""影子模式对拍基建：TraceRecorder 记录器 + JSONL 异步落盘（默认关闭，不改任何行为）。

- record(kind, data)：生产者 put_nowait 入队；队列满则丢弃并记 dropped 计数。
- 后台 daemon 线程批量 flush 到 JSONL；每 5MB 轮转一次，循环保留 3 份
  （trace_0.jsonl / trace_1.jsonl / trace_2.jsonl，覆盖最旧）。
- 每份文件首行写入 header（v/pid/clock/start_wall 等）。
- 全局自增 seq：itertools.count(1)（入队时分配，保证时序单调）。
- trace_enabled 全局开关：config/settings.yaml 的 music.trace_enabled，默认 False。

铁律：记录过程自身异常必须吞掉，绝不干扰业务线程。
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
import time

_SEQ = itertools.count(1)

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_DEFAULT_KEEP = 3                     # 保留 3 份轮转文件
_BATCH_MAX = 64                       # 单次批量写盘最大条数


def _load_trace_enabled() -> bool:
    """读取 config/settings.yaml 的 music.trace_enabled（默认 False）。读失败静默降级。"""
    try:
        import yaml
        from app.core.path_resolve import PROJECT_ROOT
        p = os.path.join(PROJECT_ROOT, "config", "settings.yaml")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return bool((data.get("music") or {}).get("trace_enabled", False))
    except Exception:
        pass
    return False


class TraceRecorder:
    """JSONL trace 记录器：入队即返回（不阻塞生产者），后台线程落盘。"""

    def __init__(
        self,
        output_dir: str | None = None,
        enabled: bool | None = None,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        keep: int = _DEFAULT_KEEP,
    ):
        self._enabled = _load_trace_enabled() if enabled is None else bool(enabled)
        self._max_bytes = int(max_bytes)
        self._keep = max(1, int(keep))
        self._output_dir = output_dir or self._default_dir()
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=2000)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._idx = 0  # 当前轮转索引（0..keep-1）
        self._last_written_path: str | None = None  # 上一次写入的文件（判断是否继续追加）
        self._start_wall = time.time()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="trace-writer")
        self._thread.start()

    @staticmethod
    def _default_dir() -> str:
        try:
            from app.core.path_resolve import PROJECT_ROOT
            return os.path.join(PROJECT_ROOT, "logs", "trace")
        except Exception:
            return os.path.join(os.getcwd(), "logs", "trace")

    # ---- 生产者接口 ----

    def record(self, kind: str, data: dict) -> None:
        """入队一条记录。开关关闭直接返回；队列满丢弃并计 dropped。"""
        if not self._enabled:
            return
        try:
            entry = {
                "seq": next(_SEQ),
                "t": time.perf_counter(),
                "th": threading.current_thread().name,
                "kind": kind,
                **data,
            }
            self._queue.put_nowait(entry)
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1

    def flush(self) -> None:
        """等待队列全部落盘（测试/退出前使用）。"""
        self._queue.join()

    @property
    def dropped(self) -> int:
        with self._dropped_lock:
            return self._dropped

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    # ---- 后台写盘 ----

    def _run(self) -> None:
        while True:
            try:
                entry = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [entry]
            while len(batch) < _BATCH_MAX:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._write_batch(batch)
            for _ in batch:
                self._queue.task_done()

    def _header(self) -> dict:
        return {
            "v": "1.0",
            "app_commit": "HEAD",
            "pid": os.getpid(),
            "clock": "perf_counter",
            "start_wall": self._start_wall,
        }

    def _current_path(self) -> str:
        return os.path.join(self._output_dir, f"trace_{self._idx}.jsonl")

    def _write_batch(self, batch: list[dict]) -> None:
        try:
            os.makedirs(self._output_dir, exist_ok=True)
            path = self._current_path()
            # 轮转覆盖：轮回到旧文件（非上一次写入目标）→ 截断重写丢弃旧内容；
            # 同一文件继续写 → 追加（header 只在文件为空时补写一次）。
            truncate = os.path.exists(path) and path != self._last_written_path
            with open(path, "w" if truncate else "a",
                      encoding="utf-8", newline="\n") as f:
                if truncate or os.path.getsize(path) == 0:
                    f.write(json.dumps(self._header(), ensure_ascii=False) + "\n")
                for e in batch:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            self._last_written_path = path
            if os.path.getsize(path) >= self._max_bytes:
                # 满 5MB → 轮转到下一份（覆盖最旧）
                self._idx = (self._idx + 1) % self._keep
        except Exception:
            # 记录失败绝不影响业务
            pass


_recorder_instance: TraceRecorder | None = None
_recorder_lock = threading.Lock()


def get_trace_recorder(enabled: bool | None = None) -> TraceRecorder:
    """全局单例。enabled=None 时按 settings.yaml 的 music.trace_enabled 决定。"""
    global _recorder_instance
    if _recorder_instance is None:
        with _recorder_lock:
            if _recorder_instance is None:
                _recorder_instance = TraceRecorder(enabled=enabled)
    return _recorder_instance
