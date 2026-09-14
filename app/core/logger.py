"""
日志系统 - JSONL 格式，按日期滚动

同时输出到控制台和文件。
"""
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional


class Logger:
    """JSONL 日志记录器"""

    def __init__(self, log_dir: str = "./logs", level: str = "INFO"):
        self.log_dir = log_dir
        self.level = self._level_to_int(level)
        self._current_date: Optional[str] = None
        self._file = None
        os.makedirs(log_dir, exist_ok=True)

    def _level_to_int(self, level: str) -> int:
        levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        return levels.get(level.upper(), 20)

    def _ensure_file(self):
        """确保当前日期的日志文件已打开"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._file:
                self._file.close()
            filepath = os.path.join(self.log_dir, f"{today}.jsonl")
            self._file = open(filepath, "a", encoding="utf-8")
            self._current_date = today

    def _write(self, level: str, message: str, extra: Optional[Dict[str, Any]] = None):
        """写入日志"""
        if self._level_to_int(level) < self.level:
            return

        self._ensure_file()

        entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message,
        }
        if extra:
            entry.update(extra)

        line = json.dumps(entry, ensure_ascii=False)

        # 写入文件
        if self._file:
            self._file.write(line + "\n")
            self._file.flush()

        # 控制台输出
        color = self._level_color(level)
        reset = "\033[0m"
        print(f"{color}[{level}]{reset} {message}")

    def _level_color(self, level: str) -> str:
        colors = {
            "DEBUG": "\033[36m",
            "INFO": "\033[32m",
            "WARNING": "\033[33m",
            "ERROR": "\033[31m",
            "CRITICAL": "\033[35m",
        }
        return colors.get(level, "")

    def debug(self, message: str, **kwargs):
        self._write("DEBUG", message, kwargs)

    def info(self, message: str, **kwargs):
        self._write("INFO", message, kwargs)

    def warning(self, message: str, **kwargs):
        self._write("WARNING", message, kwargs)

    def error(self, message: str, **kwargs):
        self._write("ERROR", message, kwargs)

    def critical(self, message: str, **kwargs):
        self._write("CRITICAL", message, kwargs)

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


# 全局日志单例
_logger: Optional[Logger] = None


def get_logger(log_dir: str = "./logs", level: str = "INFO") -> Logger:
    """获取全局日志单例"""
    global _logger
    if _logger is None:
        _logger = Logger(log_dir, level)
    return _logger
