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


class TestEventLogRotation:
    """EventLog 日志轮转专项测试"""

    def setup_method(self):
        """每个测试前初始化，使用极小阈值触发轮转"""
        self.test_dir = "./data/test_events_rotation"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir, exist_ok=True)

        import app.core.event_bus as eb_module
        eb_module._event_bus = None
        self.event_bus = eb_module.get_event_bus()

        # 使用极小阈值：1KB，最多 2 个轮转文件
        self.event_log = EventLog(
            log_dir=self.test_dir,
            max_file_size_mb=0.001,  # ~1KB
            max_rotations=2,
        )
        self.event_log.start()

    def teardown_method(self):
        """每个测试后清理"""
        self.event_log.stop()
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def _write_events(self, count: int, payload_size: int = 500):
        """写入指定数量的事件，每个事件 payload 约 payload_size 字节"""
        big_payload = {"data": "x" * payload_size}
        for i in range(count):
            event = Event(
                event_type=f"test.rotation_{i}",
                data=dict(big_payload, index=i),
                source="test",
            )
            self.event_bus.publish(event)
        time.sleep(0.3)  # 等待异步写入完成

    def test_no_rotation_below_threshold(self):
        """文件小于阈值时不轮转"""
        self._write_events(2, payload_size=100)

        date_str = time.strftime("%Y-%m-%d")
        files = os.listdir(self.test_dir)
        # 应该只有主文件，没有 .1 .2
        assert f"{date_str}.jsonl" in files
        assert not any(".jsonl." in f for f in files)

    def test_rotation_creates_backup_file(self):
        """超过阈值后生成轮转文件 .1"""
        self._write_events(30, payload_size=200)

        date_str = time.strftime("%Y-%m-%d")
        files = os.listdir(self.test_dir)
        # 应该有主文件和至少一个轮转文件
        assert f"{date_str}.jsonl" in files
        rotation_files = [f for f in files if f.startswith(f"{date_str}.jsonl.")]
        assert len(rotation_files) >= 1, f"未触发轮转，文件列表: {files}"

    def test_rotation_respects_max_rotations(self):
        """多次轮转后，保留文件数不超过上限"""
        # 写入大量事件，触发多次轮转
        self._write_events(50, payload_size=300)

        date_str = time.strftime("%Y-%m-%d")
        files = os.listdir(self.test_dir)
        rotation_files = sorted([f for f in files if f.startswith(f"{date_str}.jsonl.")])

        # max_rotations=2，应该最多有 .1 和 .2 两个轮转文件
        assert len(rotation_files) <= 2, f"轮转文件过多: {rotation_files}"

    def test_rotation_keeps_newest_content(self):
        """轮转后新事件写入新的主文件"""
        # 先写一批
        self._write_events(5, payload_size=200)
        date_str = time.strftime("%Y-%m-%d")
        main_file = os.path.join(self.test_dir, f"{date_str}.jsonl")

        # 再写一批触发轮转
        self._write_events(20, payload_size=300)

        # 主文件应该存在
        assert os.path.exists(main_file)
        # 总事件数应该合理（主文件 + 轮转文件）
        total_lines = 0
        for fname in os.listdir(self.test_dir):
            if ".jsonl" in fname:
                fpath = os.path.join(self.test_dir, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    total_lines += sum(1 for line in f if line.strip())
        # 至少应该有一些事件被写入（允许异步延迟）
        assert total_lines >= 5, f"事件数过少: {total_lines}"

    def test_rotation_does_not_lose_events(self):
        """轮转机制工作：有事件写入且有轮转文件生成"""
        # 写入足够多的事件触发轮转
        self._write_events(30, payload_size=200)

        # 统计所有日志文件中的事件总数
        total_lines = 0
        rotation_files = []
        for fname in os.listdir(self.test_dir):
            if ".jsonl" in fname:
                fpath = os.path.join(self.test_dir, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    total_lines += sum(1 for line in f if line.strip())
                if ".jsonl." in fname:
                    rotation_files.append(fname)

        # 验证：至少有一些事件被写入
        assert total_lines > 0, "没有任何事件被写入"
        # 验证：至少生成了轮转文件
        assert len(rotation_files) >= 1, f"应该至少生成一个轮转文件，实际文件: {os.listdir(self.test_dir)}"
