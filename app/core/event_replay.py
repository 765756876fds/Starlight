"""
JSONL Event Replay - 事件回放

职责：
- 读取 JSONL 文件
- 逐行恢复 Event 对象
- 按原始顺序 publish 到 EventBus

原则：
- 离线功能，不启动真实 LoL/LCU/mpv/ASR/TTS
- 不做时间间隔模拟，尽可能快地按顺序 replay
- 避免 replay 事件再次写入 EventLog（通过 _replay 标记）
"""
import json
import time
from typing import Any

from .event_bus import Event, EventBus

# Replay 标记 key（放在 Event.data 中，EventLog 检查并跳过）
REPLAY_MARKER = "_replay"


class EventReplay:
    """
    JSONL 事件回放器

    读取 JSONL 文件，恢复事件并按原始顺序 publish 到 EventBus。
    """

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._replay_count = 0
        self._skip_count = 0

    def replay_file(self, filepath: str) -> int:
        """
        回放一个 JSONL 文件

        Args:
            filepath: JSONL 文件路径

        Returns:
            实际 replay 的事件数量
        """
        self._replay_count = 0
        self._skip_count = 0

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            print(f"[EventReplay] 文件不存在: {filepath}")
            return 0
        except Exception as e:
            print(f"[EventReplay] 读取文件失败 {filepath}: {e}")
            return 0

        print(f"[EventReplay] 开始回放 {filepath}，共 {len(lines)} 行")

        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                # 空行跳过
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[EventReplay] 行 {line_num} JSON 解析失败: {e}，跳过")
                self._skip_count += 1
                continue

            # 恢复 Event
            try:
                event = self._entry_to_event(entry)
                if event is None:
                    self._skip_count += 1
                    continue

                # publish 到 EventBus
                self.event_bus.publish(event)
                self._replay_count += 1

            except Exception as e:
                print(f"[EventReplay] 行 {line_num} 恢复失败: {e}，跳过")
                self._skip_count += 1
                continue

        print(f"[EventReplay] 完成: replay {self._replay_count} 个事件，跳过 {self._skip_count} 行")
        return self._replay_count

    def _entry_to_event(self, entry: dict[str, Any]) -> Event | None:
        """
        将 JSONL entry 恢复为 Event 对象

        核心字段：event_type / timestamp / source / payload
        event_id 保留在 data 中作为 metadata，不修改 Event 模型
        """
        # 检查必要字段
        event_type = entry.get("event_type")
        if not event_type:
            print("[EventReplay] 缺少 event_type 字段，跳过")
            return None

        timestamp = entry.get("timestamp", time.time())
        source = entry.get("source", "replay")
        payload = entry.get("payload", {})

        # 复制 payload，避免修改原始数据
        data = dict(payload) if isinstance(payload, dict) else {}

        # 保留原始 event_id 作为 metadata
        if "event_id" in entry:
            data["_original_event_id"] = entry["event_id"]

        # 添加 replay 标记，EventLog 看到后跳过写入
        data[REPLAY_MARKER] = True

        # 创建 Event
        event = Event(
            event_type=event_type,
            data=data,
            timestamp=timestamp,
            source=source + " (replay)",
        )

        return event

    def get_replay_count(self) -> int:
        """获取上次 replay 的事件数"""
        return self._replay_count

    def get_skip_count(self) -> int:
        """获取上次 skip 的行数"""
        return self._skip_count
