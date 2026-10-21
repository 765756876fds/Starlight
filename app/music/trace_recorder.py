"""影子模式对拍基建：TraceRecorder 记录器 + JSONL 异步落盘（默认关闭，不改任何行为）。

- record(kind, data)：生产者 put_nowait 入队；队列满则丢弃并记 dropped 计数。
- 后台 daemon 线程批量 flush 到 JSONL；每 5MB 轮转一个新文件（n 递增）。
- 文件命名 trace_<start_wall>_<pid>_<n>.jsonl：每次启动 start_wall 不同 → 天然不覆盖
  上次运行的 trace；启动时按 mtime 保留最近 10 个文件、清理更旧的。
- 每份文件首行写入 header（v/app_commit/pid/clock/start_wall/start_perf 等）。
- 全局自增 seq：itertools.count(1)（入队时分配）。
- 数据约定：
  - t 与 seq 均为"调用完成时刻"（perf_counter / 单调序号）；
  - seq 断号 = 该序号条目被丢弃（队列满 或 序列化失败），属预期行为，不重试。
- trace_enabled 全局开关：config/settings.yaml 的 music.trace_enabled，默认 False。

铁律：记录过程自身异常必须吞掉，绝不干扰业务线程；单条坏记录只计入 dropped，
绝不影响同批其余条目。
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import threading
import time

_SEQ = itertools.count(1)

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_BATCH_MAX = 64                       # 单次批量写盘最大条数
_MAX_KEEP_FILES = 10                  # 启动时按 mtime 保留的最近 trace 文件数


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


def _read_app_commit() -> str:
    """读取真实 git 短 hash（HEAD）。不在仓库/失败 → "unknown"。"""
    try:
        from app.core.path_resolve import PROJECT_ROOT
        cwd = PROJECT_ROOT
    except Exception:
        cwd = None
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, timeout=2.0, stderr=subprocess.DEVNULL)
        text = out.decode("utf-8", errors="replace").strip()
        return text or "unknown"
    except Exception:
        return "unknown"


class TraceRecorder:
    """JSONL trace 记录器：入队即返回（不阻塞生产者），后台线程落盘。"""

    def __init__(
        self,
        output_dir: str | None = None,
        enabled: bool | None = None,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ):
        self._enabled = _load_trace_enabled() if enabled is None else bool(enabled)
        self._max_bytes = int(max_bytes)
        self._output_dir = output_dir or self._default_dir()
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=2000)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        # 每次启动唯一标识：文件名/header 均以 start_wall 为基（保证不覆盖上次 trace）
        self._start_wall = time.time()
        self._start_perf = time.perf_counter()
        self._app_commit = _read_app_commit()
        self._n = 0                      # 本次运行内文件序号（轮转递增）
        self._last_written_path: str | None = None
        self._cleanup_old_files()
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

    # ---- 启动清理：只删旧运行的 trace，本次运行文件 mtime 最新，天然保留 ----

    def _cleanup_old_files(self) -> None:
        try:
            os.makedirs(self._output_dir, exist_ok=True)
            files = []
            for name in os.listdir(self._output_dir):
                if name.startswith("trace_") and name.endswith(".jsonl"):
                    p = os.path.join(self._output_dir, name)
                    try:
                        files.append((os.path.getmtime(p), p))
                    except OSError:
                        continue
            files.sort(reverse=True)  # 新 → 旧
            for _, p in files[_MAX_KEEP_FILES:]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            pass

    def _prune_current_run(self) -> None:
        """轮转时轻量收敛：本次运行产生的文件超上限则删最旧（只删 trace_* 前缀文件）。"""
        try:
            prefix = f"trace_{int(self._start_wall)}_{os.getpid()}_"
            files = []
            for name in os.listdir(self._output_dir):
                if name.startswith(prefix) and name.endswith(".jsonl"):
                    p = os.path.join(self._output_dir, name)
                    try:
                        files.append((int(name.rsplit("_", 1)[1].split(".")[0]), p))
                    except Exception:
                        continue
            files.sort()
            for _, p in files[:-_MAX_KEEP_FILES]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            pass

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
            "app_commit": self._app_commit,
            "pid": os.getpid(),
            "clock": "perf_counter",
            "start_wall": self._start_wall,
            "start_perf": self._start_perf,
        }

    def _current_path(self) -> str:
        return os.path.join(
            self._output_dir, f"trace_{int(self._start_wall)}_{os.getpid()}_{self._n}.jsonl")

    @staticmethod
    def _dump(rec: dict) -> str | None:
        """单条序列化：任何不可序列化对象降级为类型摘要；失败返回 None（计入 dropped）。"""
        try:
            return json.dumps(rec, default=lambda o: f"<{type(o).__name__}>",
                              ensure_ascii=False)
        except Exception:
            return None

    def _write_batch(self, batch: list[dict]) -> None:
        try:
            os.makedirs(self._output_dir, exist_ok=True)
            path = self._current_path()
            new_file = path != self._last_written_path
            with open(path, "w" if new_file else "a",
                      encoding="utf-8", newline="\n") as f:
                if new_file or os.path.getsize(path) == 0:
                    f.write(json.dumps(self._header(), ensure_ascii=False) + "\n")
                for e in batch:
                    line = self._dump(e)
                    if line is None:
                        # 坏记录只计入 dropped，绝不中断整批
                        with self._dropped_lock:
                            self._dropped += 1
                        continue
                    f.write(line + "\n")
            self._last_written_path = path
            if os.path.getsize(path) >= self._max_bytes:
                # 满 5MB → 轮转新文件（n 递增，不覆盖任何已存在文件）
                self._n += 1
                self._prune_current_run()
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
