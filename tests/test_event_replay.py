"""
EventReplay 测试
"""
import pytest

pytestmark = pytest.mark.unit

import os
import json
import time
import shutil
import pytest

from app.core.event_bus import Event, EventType, EventBus
from app.core.event_log import EventLog
from app.core.event_replay import EventReplay, REPLAY_MARKER


class TestEventReplay:
    """EventReplay 测试"""

    def setup_method(self):
        """每个测试前初始化"""
        self.test_dir = "./data/test_replay"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir, exist_ok=True)

        # 重置 EventBus 单例
        import app.core.event_bus as eb_module
        eb_module._event_bus = None

        self.event_bus = eb_module.get_event_bus()
        self.events_received = []

        # 订阅所有事件
        self.event_bus.subscribe_all(self._on_event)

    def teardown_method(self):
        """每个测试后清理"""
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def _on_event(self, event: Event):
        self.events_received.append(event)

    def _write_jsonl(self, filename: str, lines: list):
        """写入测试 JSONL 文件"""
        filepath = os.path.join(self.test_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return filepath

    def _make_entry(self, event_type: str, data: dict = None, timestamp: float = None, source: str = "test"):
        """生成一个测试 entry"""
        return {
            "event_id": "test-id-" + str(int(time.time() * 1000)),
            "event_type": event_type,
            "timestamp": timestamp or time.time(),
            "source": source,
            "payload": data or {},
        }

    def test_empty_file(self):
        """空文件 → 0 events"""
        filepath = self._write_jsonl("empty.jsonl", [])
        replay = EventReplay(self.event_bus)
        count = replay.replay_file(filepath)
        assert count == 0

    def test_single_event_replay(self):
        """单事件 replay"""
        entry = self._make_entry("game.champion_kill", {"killer": "A", "victim": "B"})
        filepath = self._write_jsonl("single.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        count = replay.replay_file(filepath)

        assert count == 1
        assert len(self.events_received) == 1
        assert self.events_received[0].event_type == "game.champion_kill"

    def test_multiple_events_order(self):
        """多事件保持原始顺序"""
        entries = [
            self._make_entry("event1", {"n": 1}),
            self._make_entry("event2", {"n": 2}),
            self._make_entry("event3", {"n": 3}),
            self._make_entry("event4", {"n": 4}),
        ]
        lines = [json.dumps(e) for e in entries]
        filepath = self._write_jsonl("order.jsonl", lines)

        replay = EventReplay(self.event_bus)
        count = replay.replay_file(filepath)

        assert count == 4
        assert len(self.events_received) == 4
        assert self.events_received[0].data["n"] == 1
        assert self.events_received[1].data["n"] == 2
        assert self.events_received[2].data["n"] == 3
        assert self.events_received[3].data["n"] == 4

    def test_event_type_correct(self):
        """event_type 正确恢复"""
        entry = self._make_entry("lcu.gameflow_changed", {"old": "Lobby"})
        filepath = self._write_jsonl("type.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        replay.replay_file(filepath)

        assert self.events_received[0].event_type == "lcu.gameflow_changed"

    def test_timestamp_correct(self):
        """timestamp 正确恢复"""
        ts = 1234567890.123
        entry = self._make_entry("test.event", timestamp=ts)
        filepath = self._write_jsonl("ts.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        replay.replay_file(filepath)

        assert self.events_received[0].timestamp == ts

    def test_source_correct(self):
        """source 正确恢复"""
        entry = self._make_entry("test.event", source="lol")
        filepath = self._write_jsonl("source.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        replay.replay_file(filepath)

        assert "lol" in self.events_received[0].source

    def test_payload_correct(self):
        """payload 正确恢复"""
        payload = {"killer": "A", "victim": "B", "kills": 5}
        entry = self._make_entry("game.champion_kill", payload)
        filepath = self._write_jsonl("payload.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        replay.replay_file(filepath)

        event = self.events_received[0]
        assert event.data["killer"] == "A"
        assert event.data["victim"] == "B"
        assert event.data["kills"] == 5

    def test_blank_lines_skipped(self):
        """空行跳过"""
        entry = self._make_entry("test.event")
        lines = [
            "",
            json.dumps(entry),
            "",
            "",
            json.dumps(entry),
            "",
        ]
        filepath = self._write_jsonl("blank.jsonl", lines)

        replay = EventReplay(self.event_bus)
        count = replay.replay_file(filepath)

        assert count == 2  # 2 个有效事件

    def test_malformed_json_skipped(self):
        """malformed JSON 跳过并继续"""
        entry1 = self._make_entry("event1")
        entry2 = self._make_entry("event2")

        lines = [
            json.dumps(entry1),
            "{invalid json}",
            json.dumps(entry2),
        ]
        filepath = self._write_jsonl("malformed.jsonl", lines)

        replay = EventReplay(self.event_bus)
        count = replay.replay_file(filepath)

        assert count == 2  # 2 个有效事件
        assert replay.get_skip_count() == 1  # 1 行 malformed

    def test_missing_event_type_skipped(self):
        """缺少 event_type 跳过"""
        bad_entry = {"timestamp": time.time(), "payload": {}}  # 没有 event_type
        good_entry = self._make_entry("good.event")

        lines = [
            json.dumps(bad_entry),
            json.dumps(good_entry),
        ]
        filepath = self._write_jsonl("no_type.jsonl", lines)

        replay = EventReplay(self.event_bus)
        count = replay.replay_file(filepath)

        assert count == 1
        assert replay.get_skip_count() == 1

    def test_unknown_event_type_no_crash(self):
        """未知 event_type 不导致崩溃"""
        entry = self._make_entry("some.future.unknown_event", {"x": 1})
        filepath = self._write_jsonl("unknown.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        count = replay.replay_file(filepath)

        assert count == 1  # 不崩溃，仍然 replay

    def test_eventbus_subscriber_receives(self):
        """EventBus subscriber 能收到 replay event"""
        received = []

        def subscriber(event: Event):
            received.append(event)

        self.event_bus.subscribe("test.subscribed_event", subscriber)

        entry = self._make_entry("test.subscribed_event", {"msg": "hello"})
        filepath = self._write_jsonl("sub.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        replay.replay_file(filepath)

        assert len(received) == 1
        assert received[0].data["msg"] == "hello"

    def test_replay_marker_added(self):
        """replay 事件带 _replay 标记"""
        entry = self._make_entry("test.event")
        filepath = self._write_jsonl("marker.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        replay.replay_file(filepath)

        assert self.events_received[0].data[REPLAY_MARKER] is True

    def test_replay_no_duplicate_eventlog(self):
        """Replay 不导致 EventLog 再写一遍"""
        # 启动 EventLog
        event_log = EventLog(log_dir=self.test_dir + "/logs")
        event_log.start()

        # 先发布一个真实事件
        self.event_bus.publish(Event(event_type="real.event", data={"n": 1}, source="test"))
        time.sleep(0.1)

        # 创建一个 JSONL 文件，包含 2 个事件
        entries = [
            self._make_entry("replay.event1", {"n": 2}),
            self._make_entry("replay.event2", {"n": 3}),
        ]
        lines = [json.dumps(e) for e in entries]
        jsonl_path = self._write_jsonl("replay.jsonl", lines)

        # Replay
        replay = EventReplay(self.event_bus)
        count = replay.replay_file(jsonl_path)
        time.sleep(0.1)

        # 停止 EventLog
        event_log.stop()

        # 检查日志文件：应该只有 1 个真实事件（real.event）
        # replay 的 2 个事件不应该被写入
        date_str = time.strftime("%Y-%m-%d")
        log_file = os.path.join(self.test_dir + "/logs", f"{date_str}.jsonl")

        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            real_events = [json.loads(l) for l in lines if l.strip()]

        # 应该只有 1 个真实事件，没有 replay 事件
        assert len(real_events) == 1
        assert real_events[0]["event_type"] == "real.event"

    def test_return_correct_count(self):
        """返回正确 replay count"""
        entries = [self._make_entry(f"event{i}") for i in range(5)]
        lines = [json.dumps(e) for e in entries]
        filepath = self._write_jsonl("count.jsonl", lines)

        replay = EventReplay(self.event_bus)
        count = replay.replay_file(filepath)

        assert count == 5

    def test_file_not_found(self):
        """文件不存在返回 0"""
        replay = EventReplay(self.event_bus)
        count = replay.replay_file("nonexistent.jsonl")
        assert count == 0

    def test_original_event_id_preserved(self):
        """原始 event_id 保留在 data 中"""
        original_id = "abc-123-original-uuid"
        entry = self._make_entry("test.event")
        entry["event_id"] = original_id
        filepath = self._write_jsonl("eid.jsonl", [json.dumps(entry)])

        replay = EventReplay(self.event_bus)
        replay.replay_file(filepath)

        assert self.events_received[0].data["_original_event_id"] == original_id
