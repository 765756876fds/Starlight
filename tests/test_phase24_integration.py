"""
Phase 2.4 集成测试：2999 → StateDiff → EventBus → SpeechJudge → SpeechQueue
"""
import pytest

pytestmark = pytest.mark.unit

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.event_bus import get_event_bus, Event, EventType
from app.speech.speech_judge import SpeechJudge
from app.speech.speech_queue import SpeechQueue, SpeechItem, PRIORITY_B


class FakeTTS:
    def __init__(self):
        self.playing = False
        self.current_text = None
        self.speak_count = 0
        self._stop_thread = None

    def speak(self, text):
        self.current_text = text
        self.playing = True
        self.speak_count += 1
        # 模拟 0.3 秒后自动播放完
        def _finish():
            time.sleep(0.3)
            self.playing = False
        import threading
        t = threading.Thread(target=_finish, daemon=True)
        t.start()

    def stop_playback(self):
        self.playing = False

    def is_playing(self):
        return self.playing

    def start(self):
        pass

    def stop(self):
        self.playing = False


class TestPhase24Integration(unittest.TestCase):
    def setUp(self):
        self.bus = get_event_bus()
        self.tts = FakeTTS()
        self.queue = SpeechQueue(self.tts)
        self.queue.start()
        self.judge = SpeechJudge(
            {"intensity": "normal", "min_level": "B"},
            queue=self.queue,
        )
        self.judge.start()

    def tearDown(self):
        self.judge.stop()
        self.queue.shutdown()

    def test_1_champion_kill_full_chain(self):
        """Test 1：ALARM_TRIGGERED 完整链路"""
        event = Event(
            event_type=EventType.ALARM_TRIGGERED,
            data={
                "alarm": {"title": "40秒后小龙刷新"},
                "remaining": 5,
            },
            source="test"
        )
        self.bus.publish(event)
        time.sleep(0.2)
        self.assertGreaterEqual(self.queue.size() + (1 if self.queue.get_current() else 0), 1)

    def test_2_same_snapshot_no_duplicate(self):
        """Test 2：相同事件不重复播报"""
        self.judge.min_interval = 0

        alarm = {"title": "40秒后小龙刷新", "alarm_id": "same"}
        event1 = Event(
            event_type=EventType.ALARM_TRIGGERED,
            data={"alarm": alarm, "remaining": 5},
            source="test"
        )
        self.bus.publish(event1)
        time.sleep(0.1)

        event2 = Event(
            event_type=EventType.ALARM_TRIGGERED,
            data={"alarm": alarm, "remaining": 5},
            source="test"
        )
        self.bus.publish(event2)
        time.sleep(0.1)

        self.assertEqual(self.tts.speak_count, 1)

    def test_3_different_kills(self):
        """Test 3：两个不同的 alarm 事件"""
        self.judge.min_interval = 0

        event1 = Event(
            event_type=EventType.ALARM_TRIGGERED,
            data={"alarm": {"title": "A", "alarm_id": "1"}, "remaining": 5, "game_time": 100},
            source="test"
        )
        self.bus.publish(event1)
        time.sleep(0.8)

        event2 = Event(
            event_type=EventType.ALARM_TRIGGERED,
            data={"alarm": {"title": "B", "alarm_id": "2"}, "remaining": 5, "game_time": 200},
            source="test"
        )
        self.bus.publish(event2)
        time.sleep(0.8)

        self.assertEqual(self.tts.speak_count, 2)

    def test_4_same_killers_different_times(self):
        """Test 4：相同 killer/victim，不同 game_time → 不同 event_id"""
        self.judge.min_interval = 0

        event1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "A",
                "victim_name": "B",
                "game_time": 123.4,
            },
            source="test"
        )
        id1 = self.judge._get_event_id(event1)

        event2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "A",
                "victim_name": "B",
                "game_time": 245.8,
            },
            source="test"
        )
        id2 = self.judge._get_event_id(event2)

        # 应该是不同的 event_id（game_time 参与了）
        self.assertNotEqual(id1, id2)

    def test_5_speech_item_fields(self):
        """Test 5：SpeechItem 字段正确"""
        self.judge.min_interval = 0

        event = Event(
            event_type=EventType.ALARM_TRIGGERED,
            data={
                "alarm": {"title": "40秒后小龙刷新"},
                "remaining": 5,
            },
            source="test"
        )
        self.bus.publish(event)
        time.sleep(0.2)

        # 检查 TTS 是否收到了正确的文本
        self.assertEqual(self.tts.speak_count, 1)
        self.assertIn("小龙", self.tts.current_text or "")


if __name__ == "__main__":
    unittest.main()
