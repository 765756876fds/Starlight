"""
EventLog 测试
"""
import pytest

pytestmark = pytest.mark.unit

import os
import json
import time
import shutil
import pytest

from app.core.event_bus import Event, EventType, EventPriority
from app.core.event_log import EventLog, _sanitize_dict


class TestSanitize:
    """敏感信息脱敏测试"""

    def test_sanitize_token(self):
        """token 被脱敏"""
        data = {"token": "secret123", "other": "value"}
        result = _sanitize_dict(data)
        assert result["token"] == "***REDACTED***"
        assert result["other"] == "value"

    def test_sanitize_auth_token(self):
        """auth_token 被脱敏"""
        data = {"auth_token": "abc123", "name": "test"}
        result = _sanitize_dict(data)
        assert result["auth_token"] == "***REDACTED***"

    def test_sanitize_nested(self):
        """嵌套字典中的敏感字段被脱敏"""
        data = {"nested": {"password": "secret", "ok": True}}
        result = _sanitize_dict(data)
        assert result["nested"]["password"] == "***REDACTED***"
        assert result["nested"]["ok"] is True

    def test_sanitize_list(self):
        """列表中的敏感字段被脱敏"""
        data = {"items": [{"token": "secret"}, {"name": "ok"}]}
        result = _sanitize_dict(data)
        assert result["items"][0]["token"] == "***REDACTED***"
        assert result["items"][1]["name"] == "ok"


class TestEventLog:
    """EventLog 功能测试"""

    def setup_method(self):
        """每个测试前初始化"""
        self.test_dir = "./data/test_events"
        # 清理测试目录
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir, exist_ok=True)

        # 重置 EventBus 单例
        import app.core.event_bus as eb_module
        eb_module._event_bus = None

        self.event_bus = eb_module.get_event_bus()
        self.event_log = EventLog(log_dir=self.test_dir)
        self.event_log.start()

    def teardown_method(self):
        """每个测试后清理"""
        self.event_log.stop()
        # 清理测试目录
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def _publish_event(self, event_type: str, data: dict = None):
        """发布一个测试事件"""
        event = Event(
            event_type=event_type,
            data=data or {},
            source="test",
        )
        self.event_bus.publish(event)
        time.sleep(0.05)  # 等待异步写入

    def test_single_event_written(self):
        """单事件写入"""
        self._publish_event("test.event1", {"key": "value"})

        count = self.event_log.count_events()
        assert count == 1

    def test_jsonl_each_line_valid_json(self):
        """JSONL 每行是合法 JSON"""
        self._publish_event("test.event1", {"key": "value"})
        self._publish_event("test.event2", {"num": 42})

        date_str = time.strftime("%Y-%m-%d")
        filepath = os.path.join(self.test_dir, f"{date_str}.jsonl")

        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    assert "event_id" in entry
                    assert "event_type" in entry
                    assert "timestamp" in entry
                    assert "payload" in entry

    def test_event_id_unique(self):
        """每个事件有唯一 event_id"""
        self._publish_event("test.event1", {"n": 1})
        self._publish_event("test.event2", {"n": 2})

        date_str = time.strftime("%Y-%m-%d")
        filepath = os.path.join(self.test_dir, f"{date_str}.jsonl")

        ids = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    ids.append(entry["event_id"])

        assert len(ids) == 2
        assert ids[0] != ids[1]

    def test_event_type_correct(self):
        """event_type 正确保存"""
        self._publish_event("test.music_next", {"action": "next"})

        date_str = time.strftime("%Y-%m-%d")
        filepath = os.path.join(self.test_dir, f"{date_str}.jsonl")

        with open(filepath, "r", encoding="utf-8") as f:
            line = f.readline().strip()
            entry = json.loads(line)
            assert entry["event_type"] == "test.music_next"
            assert entry["payload"]["action"] == "next"

    def test_multiple_events_append(self):
        """多事件追加写入"""
        for i in range(5):
            self._publish_event(f"test.event_{i}", {"index": i})

        count = self.event_log.count_events()
        assert count == 5

    def test_date_file_naming(self):
        """日期文件命名正确"""
        self._publish_event("test.event1", {})

        date_str = time.strftime("%Y-%m-%d")
        expected_file = f"{date_str}.jsonl"
        files = os.listdir(self.test_dir)
        assert expected_file in files

    def test_directory_auto_create(self):
        """目录不存在时自动创建"""
        # 使用一个不存在的目录
        new_dir = "./data/test_events_new"
        if os.path.exists(new_dir):
            shutil.rmtree(new_dir)

        log = EventLog(log_dir=new_dir)
        log.start()
        log._on_event(Event(event_type="test.event", data={}, source="test"))
        log.stop()

        assert os.path.exists(new_dir)
        shutil.rmtree(new_dir)

    def test_json_serialize_error_no_crash(self):
        """JSON 不可序列化时不崩溃"""
        # 发布一个包含不可序列化对象的事件
        event = Event(
            event_type="test.bad_event",
            data={"obj": object()},
            source="test",
        )
        # 不应该崩溃
        self.event_bus.publish(event)
        time.sleep(0.1)

    def test_sensitive_data_redacted(self):
        """敏感信息不会进入日志"""
        self._publish_event("test.login", {
            "token": "secret123",
            "username": "user",
        })

        date_str = time.strftime("%Y-%m-%d")
        filepath = os.path.join(self.test_dir, f"{date_str}.jsonl")

        with open(filepath, "r", encoding="utf-8") as f:
            line = f.readline().strip()
            entry = json.loads(line)
            # token 应该被脱敏
            assert entry["payload"]["token"] == "***REDACTED***"
            assert entry["payload"]["username"] == "user"

    def test_eventbus_integration(self):
        """EventBus → EventLog 集成测试"""
        # 发布多个事件
        self._publish_event("game.started", {"game_time": 0})
        self._publish_event("music.next", {"song": "test"})
        self._publish_event("lcu.gameflow_changed", {"old": "Lobby", "new": "InProgress"})

        count = self.event_log.count_events()
        assert count == 3

    def test_payload_correct(self):
        """payload 正确保存"""
        payload = {
            "killer": "A",
            "victim": "B",
            "kills": 5,
        }
        self._publish_event("game.champion_kill", payload)

        date_str = time.strftime("%Y-%m-%d")
        filepath = os.path.join(self.test_dir, f"{date_str}.jsonl")

        with open(filepath, "r", encoding="utf-8") as f:
            line = f.readline().strip()
            entry = json.loads(line)
            assert entry["payload"]["killer"] == "A"
            assert entry["payload"]["victim"] == "B"
            assert entry["payload"]["kills"] == 5
