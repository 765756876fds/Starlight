"""
LCU current-summoner 测试
"""
import pytest

pytestmark = pytest.mark.unit

import pytest
from unittest.mock import MagicMock, patch

from app.core.event_bus import EventBus, Event, EventType
from app.lol.runtime import LoLRuntime


class MockLCU:
    """Mock LCU 连接"""
    def __init__(self):
        self.connected = False
        self._phase = "None"
        self._summoner = None

    def get_gameflow_phase(self):
        return self._phase

    def get_current_summoner(self):
        return self._summoner

    def start(self):
        pass

    def stop(self):
        pass


class TestLCUSummoner:
    """LCU current-summoner 测试"""

    def setup_method(self):
        """每个测试前初始化"""
        self.event_bus = EventBus()
        self.events_received = []

        # 订阅 LCU 事件
        self.event_bus.subscribe(EventType.LCU_SUMMONER_UPDATED, self._on_event)
        self.event_bus.subscribe(EventType.LCU_CONNECTED, self._on_event)
        self.event_bus.subscribe(EventType.LCU_DISCONNECTED, self._on_event)

        # 创建 LoLRuntime
        with patch('app.lol.runtime.LiveClientAPI'), \
             patch('app.lol.runtime.GameAlarmEngine'), \
             patch('app.lol.runtime.SnapshotBuilder'), \
             patch('app.lol.runtime.StateDiff'):
            self.runtime = LoLRuntime({})

        # 替换 LCU
        self.mock_lcu = MockLCU()
        self.runtime.lcu = self.mock_lcu
        self.runtime.event_bus = self.event_bus
        self.runtime._running = True

    def _on_event(self, event: Event):
        self.events_received.append(event)

    def _get_summoner_events(self):
        return [e for e in self.events_received if e.event_type == EventType.LCU_SUMMONER_UPDATED]

    def test_first_read(self):
        """首次成功读取"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "TestPlayer",
            "profileIconId": 1234,
            "summonerLevel": 100,
            "puuid": "abc-123-puuid",
        }

        self.runtime._poll_lcu()

        events = self._get_summoner_events()
        assert len(events) == 1
        assert events[0].data["is_first"] is True
        assert events[0].data["summoner"]["summonerId"] == 12345
        assert events[0].data["summoner"]["displayName"] == "TestPlayer"

    def test_same_data_no_duplicate(self):
        """相同数据不重复 publish"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "TestPlayer",
            "summonerLevel": 100,
        }

        self.runtime._poll_lcu()  # 第一次
        self.events_received.clear()

        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "TestPlayer",
            "summonerLevel": 100,
        }
        self.runtime._poll_lcu()  # 第二次，相同

        events = self._get_summoner_events()
        assert len(events) == 0

    def test_data_change_publishes(self):
        """数据变化 publish"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "TestPlayer",
            "summonerLevel": 100,
        }
        self.runtime._poll_lcu()
        self.events_received.clear()

        # 等级变化
        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "TestPlayer",
            "summonerLevel": 101,
        }
        self.runtime._poll_lcu()

        events = self._get_summoner_events()
        assert len(events) == 1
        assert events[0].data["is_first"] is False
        assert events[0].data["summoner"]["summonerLevel"] == 101

    def test_display_name_change(self):
        """显示名变化 publish"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "OldName",
            "summonerLevel": 100,
        }
        self.runtime._poll_lcu()
        self.events_received.clear()

        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "NewName",
            "summonerLevel": 100,
        }
        self.runtime._poll_lcu()

        events = self._get_summoner_events()
        assert len(events) == 1
        assert events[0].data["summoner"]["displayName"] == "NewName"

    def test_http_error_no_crash(self):
        """HTTP 错误不 crash"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu.get_current_summoner = MagicMock(side_effect=Exception("HTTP 500"))

        self.runtime._poll_lcu()  # 不应该崩溃

    def test_disconnect_no_crash(self):
        """LCU disconnect 不 crash"""
        self.mock_lcu.connected = False
        self.mock_lcu._summoner = None

        self.runtime._poll_lcu()  # 不应该崩溃

    def test_reconnect_re_read(self):
        """reconnect 后重新读取"""
        # 断开
        self.mock_lcu.connected = False
        self.mock_lcu._summoner = None
        self.runtime._poll_lcu()
        self.events_received.clear()

        # 重连
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = {
            "summonerId": 99999,
            "displayName": "NewPlayer",
            "summonerLevel": 50,
        }
        self.runtime._poll_lcu()

        events = self._get_summoner_events()
        assert len(events) == 1
        assert events[0].data["summoner"]["summonerId"] == 99999

    def test_empty_response(self):
        """空 response 不更新"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = None

        self.runtime._poll_lcu()

        events = self._get_summoner_events()
        assert len(events) == 0

    def test_null_response(self):
        """null response 不更新"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = None

        self.runtime._poll_lcu()

        events = self._get_summoner_events()
        assert len(events) == 0

    def test_missing_optional_fields(self):
        """缺少可选字段不崩溃"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        # 只有基本字段，没有 puuid/gameName/tagLine
        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "TestPlayer",
        }

        self.runtime._poll_lcu()  # 不应该崩溃

        events = self._get_summoner_events()
        assert len(events) == 1
        assert events[0].data["summoner"]["summonerId"] == 12345
        assert events[0].data["summoner"]["puuid"] is None

    def test_tencent_style_payload(self):
        """Tencent 风格真实 payload"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        # 模拟 Tencent LCU 可能返回的字段
        self.mock_lcu._summoner = {
            "summonerId": 200123456,
            "displayName": "测试玩家",
            "profileIconId": 5432,
            "summonerLevel": 187,
            "puuid": "abc-123-def-456",
            "gameName": "测试玩家",
            "tagLine": "CN1",
            "accountId": 100123456,
            "internal": "some_internal_field",  # 多余字段应该被忽略
        }

        self.runtime._poll_lcu()

        events = self._get_summoner_events()
        assert len(events) == 1
        summoner = events[0].data["summoner"]
        assert summoner["summonerId"] == 200123456
        assert summoner["displayName"] == "测试玩家"
        assert summoner["profileIconId"] == 5432
        assert summoner["summonerLevel"] == 187
        assert summoner["puuid"] == "abc-123-def-456"
        assert summoner["gameName"] == "测试玩家"
        assert summoner["tagLine"] == "CN1"
        # 多余字段不应该在提取结果中
        assert "accountId" not in summoner

    def test_eventbus_integration(self):
        """EventBus 集成测试"""
        received_events = []

        def subscriber(event: Event):
            received_events.append(event)

        self.event_bus.subscribe(EventType.LCU_SUMMONER_UPDATED, subscriber)

        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "TestPlayer",
            "summonerLevel": 100,
        }

        self.runtime._poll_lcu()

        assert len(received_events) == 1
        assert received_events[0].data["summoner"]["displayName"] == "TestPlayer"

    def test_state_store_integration(self):
        """StateStore 集成测试"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.mock_lcu._summoner = {
            "summonerId": 12345,
            "displayName": "TestPlayer",
            "summonerLevel": 100,
        }

        self.runtime._poll_lcu()

        # 检查 StateStore
        stored = self.runtime.state_store.get("lcu", "current_summoner")
        assert stored is not None
        assert stored["summonerId"] == 12345
        assert stored["displayName"] == "TestPlayer"
