"""
stdout/stderr 重定向：转发到 UI 信号 + 按日滚动落盘。

日志轨道约定：
- logs/print-{date}.log   ← stdout 通道的 print 输出（调试输出，无调用位置信息）
- logs/error-{date}.log   ← stderr 通道的非日志输出（traceback.print_exc / 未捕获异常等）
- logs/app.log            ← 标准库 logging 体系（logging.getLogger(...) 的输出，结构化）

注意：print 落盘只有字符串、无调用位置信息，长期应向 logger 体系迁移。
stdout 与 stderr 各开一个实例、各写各的文件，避免同文件抢锁交错。
"""
import os
import sys
import threading
from datetime import datetime

from app.core.log_bus import get_log_bus


class TeeStream:
    def __init__(self, original=None, log_dir="./logs", filename_prefix="print"):
        self._original = original
        self._bus = get_log_bus()
        self._buffer = ""
        self._log_dir = log_dir
        self._prefix = filename_prefix
        self._lock = threading.Lock()
        self._file = None
        self._date = None
        os.makedirs(log_dir, exist_ok=True)   # 幂等：install 时 logs/ 未必存在

    def _ensure_file(self):
        """按日期滚动：logs/{prefix}-{date}.log"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._date:
            if self._file:
                self._file.close()
            path = os.path.join(self._log_dir, f"{self._prefix}-{today}.log")
            self._file = open(path, "a", encoding="utf-8")  # 文件固定 UTF-8
            self._date = today
            # 滚动时清理 30 天前的旧文件（同 prefix），防无限累积
            try:
                import glob
                pattern = os.path.join(self._log_dir, f"{self._prefix}-*.log")
                files = sorted(glob.glob(pattern))
                for old in files[:-30]:
                    try:
                        os.remove(old)
                    except Exception:
                        pass
            except Exception:
                pass

    def write(self, data):
        if self._original:
            try:
                self._original.write(data)
            except Exception:
                pass
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                ts = datetime.now().strftime("%H:%M:%S")
                # 落盘：加锁 + 滚动 + UTF-8；失败静默（绝不能再 print，防递归）
                with self._lock:
                    try:
                        self._ensure_file()
                        if self._file:
                            self._file.write(f"[{ts}] {line}\n")
                            self._file.flush()
                    except Exception:
                        pass
                # UI 信号：与落盘分开保护
                try:
                    self._bus.sig_log.emit(f"[{ts}] {line}")
                except Exception:
                    pass
        return len(data)

    def flush(self):
        if self._original:
            try:
                self._original.flush()
            except Exception:
                pass
        with self._lock:
            if self._file:
                try:
                    self._file.flush()
                except Exception:
                    pass

    def close(self):
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._file = None
            self._date = None   # 重置：close 后若再有输出，下次 write 会重新开文件

    def isatty(self):
        return False


_streams = []


def install_log_redirect(log_dir="./logs"):
    global _streams
    out = TeeStream(sys.stdout, log_dir, filename_prefix="print")
    err = TeeStream(sys.stderr, log_dir, filename_prefix="error")
    _streams = [out, err]
    sys.stdout = out
    sys.stderr = err
    return _streams


def close_redirect():
    """进程收尾：flush+close 两个落盘句柄（Application.stop 末尾调用）"""
    for s in _streams:
        try:
            s.close()
        except Exception:
            pass
