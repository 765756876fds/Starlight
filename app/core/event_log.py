"""
JSONL Event Log - 事件持久化层

职责：
- 订阅 EventBus 所有事件
- 追加写入 JSONL 文件
- 按日期分文件
- 异常隔离：写入失败不影响 EventBus

原则：
- EventLog 是观察层，不是核心链路
- 即使写入失败，也不能导致 Agent 崩溃
"""
import os
import json
import uuid
import time
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from .event_bus import Event, EventBus, get_event_bus


# 需要脱敏的敏感字段
SENSITIVE_KEYS = {
    "token", "auth_token", "remoting_auth_token",
    "password", "secret", "api_key", "apikey",
    "authorization", "cookie", "session",
}


def _sanitize_dict(d: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    """递归脱敏字典中的敏感字段"""
    if depth > 5:  # 防止无限递归
        return d

    result = {}
    for key, value in d.items():
        key_lower = key.lower()
        if any(s in key_lower for s in SENSITIVE_KEYS):
            result[key] = "***REDACTED***"
        elif isinstance(value, dict):
            result[key] = _sanitize_dict(value, depth + 1)
        elif isinstance(value, list):
            result[key] = [
                _sanitize_dict(item, depth + 1) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


class EventLog:
    """
    JSONL 事件日志

    订阅 EventBus 所有事件，追加写入按日期分的 JSONL 文件。
    """

    def __init__(self, log_dir: str = "./data/events"):
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self._current_date: Optional[str] = None
        self._current_file = None
        self._running = False

        # 确保目录存在
        os.makedirs(self.log_dir, exist_ok=True)

    def start(self):
        """启动 EventLog，订阅 EventBus"""
        if self._running:
            return

        self._running = True
        event_bus = get_event_bus()
        event_bus.subscribe_all(self._on_event)
        print(f"[EventLog] 已启动，日志目录: {self.log_dir}")

    def stop(self):
        """停止 EventLog，关闭文件"""
        self._running = False
        self._close_file()

    def _on_event(self, event: Event):
        """
        EventBus 事件回调

        所有异常都被捕获，确保不影响 EventBus。
        Replay 事件（带 _replay 标记）不写入日志，避免循环。
        """
        if not self._running:
            return

        # 跳过 replay 事件，避免 Replay → EventBus → EventLog 循环
        if event.data.get("_replay", False):
            return

        try:
            self._append_event(event)
        except Exception as e:
            # 绝对不能因为日志写入失败导致 EventBus 崩溃
            print(f"[EventLog] 写入失败: {e}")

    def _append_event(self, event: Event):
        """追加写入一个事件"""
        # 生成 event_id
        event_id = str(uuid.uuid4())

        # 脱敏 payload
        safe_data = _sanitize_dict(event.data)

        # 构建日志条目
        entry = {
            "event_id": event_id,
            "event_type": event.event_type,
            "timestamp": event.timestamp,
            "source": event.source,
            "payload": safe_data,
        }

        # 序列化 JSON
        try:
            line = json.dumps(entry, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            print(f"[EventLog] JSON 序列化失败 {event.event_type}: {e}")
            # 尝试写入简化版本
            entry = {
                "event_id": event_id,
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                "source": event.source,
                "payload": {"_error": "unserializable"},
            }
            line = json.dumps(entry, ensure_ascii=False)

        # 获取当前日期文件
        date_str = datetime.now().strftime("%Y-%m-%d")
        filepath = os.path.join(self.log_dir, f"{date_str}.jsonl")

        # 打开/切换文件（加锁）
        with self._lock:
            if self._current_date != date_str:
                self._close_file()
                self._current_date = date_str
                self._current_file = open(filepath, "a", encoding="utf-8")

            # 写入一行
            self._current_file.write(line + "\n")
            self._current_file.flush()

    def _close_file(self):
        """关闭当前文件"""
        if self._current_file:
            try:
                self._current_file.close()
            except Exception:
                pass
            self._current_file = None
        self._current_date = None

    def get_log_dir(self) -> str:
        """获取日志目录"""
        return self.log_dir

    def count_events(self, date_str: Optional[str] = None) -> int:
        """统计某天的事件数（只读，用于测试）"""
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        filepath = os.path.join(self.log_dir, f"{date_str}.jsonl")
        if not os.path.exists(filepath):
            return 0

        count = 0
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except Exception:
            pass
        return count
