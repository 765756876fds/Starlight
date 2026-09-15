"""
LCU GameFlow → EventBus 测试
"""
import pytest
from unittest.mock import MagicMock, patch

from app.core.event_bus import EventBus, Event, EventType
from app.lol.runtime import LoLRuntime


class MockLCU:
    """Mock LCU 连接"""
    def __init__(self):
        self.connected = False
        self._phase = "None"

    def get_gameflow_phase(self):
        return self._phase

    def start(self):
        pass

    def stop(self):
        pass


class TestLCUGameFlow:
    """LCU GameFlow 事件测试"""

    def setup_method(self):
        """每个测试前初始化"""
        self.event_bus = EventBus()
        self.events_received = []

        # 订阅 LCU 事件
        self.event_bus.subscribe(EventType.LCU_GAMEFLOW_CHANGED, self._on_event)
        self.event_bus.subscribe(EventType.LCU_CONNECTED, self._on_event)
        self.event_bus.subscribe(EventType.LCU_DISCONNECTED, self._on_event)

        # 创建 LoLRuntime（不真正 start）
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

    def _get_gameflow_events(self):
        return [e for e in self.events_received if e.event_type == EventType.LCU_GAMEFLOW_CHANGED]

    def test_first_phase_change(self):
        """UNKNOWN → LOBBY 产生 1 个事件"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"

        self.runtime._poll_lcu()

        events = self._get_gameflow_events()
        assert len(events) == 1
        assert events[0].data["old_phase"] == "UNKNOWN"
        assert events[0].data["new_phase"] == "Lobby"

    def test_same_phase_no_event(self):
        """LOBBY → LOBBY 不产生事件"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.runtime._poll_lcu()  # 第一次
        self.events_received.clear()

        self.mock_lcu._phase = "Lobby"
        self.runtime._poll_lcu()  # 第二次，相同

        events = self._get_gameflow_events()
        assert len(events) == 0

    def test_phase_change(self):
        """LOBBY → CHAMP_SELECT 产生 1 个事件"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.runtime._poll_lcu()
        self.events_received.clear()

        self.mock_lcu._phase = "ChampSelect"
        self.runtime._poll_lcu()

        events = self._get_gameflow_events()
        assert len(events) == 1
        assert events[0].data["old_phase"] == "Lobby"
        assert events[0].data["new_phase"] == "ChampSelect"

    def test_unknown_phase_not_crash(self):
        """未知 phase 不崩溃，归类为 Unknown"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "SomeFuturePhase"
        self.runtime._poll_lcu()

        events = self._get_gameflow_events()
        assert len(events) == 1
        assert events[0].data["new_phase"] == "Unknown"
        assert events[0].data["raw_phase"] == "SomeFuturePhase"

    def test_lcu_disconnect(self):
        """LCU 断开不崩溃"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.runtime._poll_lcu()
        self.events_received.clear()

        self.mock_lcu.connected = False
        self.runtime._poll_lcu()

        # 应该发布 disconnect 事件
        disconnect_events = [e for e in self.events_received if e.event_type == EventType.LCU_DISCONNECTED]
        assert len(disconnect_events) == 1

    def test_lcu_reconnect(self):
        """LCU 恢复后继续 polling"""
        # 断开
        self.mock_lcu.connected = False
        self.runtime._poll_lcu()
        self.events_received.clear()

        # 重连
        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.runtime._poll_lcu()

        connected_events = [e for e in self.events_received if e.event_type == EventType.LCU_CONNECTED]
        gameflow_events = self._get_gameflow_events()
        assert len(connected_events) == 1
        assert len(gameflow_events) == 1

    def test_get_phase_exception(self):
        """get_gameflow_phase 抛异常不崩溃"""
        self.mock_lcu.connected = True
        self.mock_lcu.get_gameflow_phase = MagicMock(side_effect=Exception("Connection error"))

        self.runtime._poll_lcu()  # 不应该崩溃

    def test_empty_phase(self):
        """空 phase 不产生事件"""
        self.mock_lcu.connected = True
        self.mock_lcu._phase = ""
        self.runtime._poll_lcu()

        events = self._get_gameflow_events()
        assert len(events) == 0

    def test_multiple_phase_changes(self):
        """多次 phase 变化都产生事件"""
        self.mock_lcu.connected = True

        phases = ["Lobby", "ChampSelect", "InProgress", "WaitingForStats", "Lobby"]
        for phase in phases:
            self.mock_lcu._phase = phase
            self.runtime._poll_lcu()

        events = self._get_gameflow_events()
        # 第一次 + 4 次变化 = 5 个事件
        assert len(events) == 5
        assert events[0].data["new_phase"] == "Lobby"
        assert events[1].data["old_phase"] == "Lobby"
        assert events[1].data["new_phase"] == "ChampSelect"
        assert events[2].data["old_phase"] == "ChampSelect"
        assert events[2].data["new_phase"] == "InProgress"

    def test_eventbus_integration(self):
        """EventBus 集成测试：subscriber 能收到事件"""
        received_events = []

        def subscriber(event: Event):
            received_events.append(event)

        self.event_bus.subscribe(EventType.LCU_GAMEFLOW_CHANGED, subscriber)

        self.mock_lcu.connected = True
        self.mock_lcu._phase = "Lobby"
        self.runtime._poll_lcu()

        assert len(received_events) == 1
        assert received_events[0].data["new_phase"] == "Lobby"
