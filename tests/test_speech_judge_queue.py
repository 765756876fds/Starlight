"""
SpeechJudge → SpeechQueue 接线测试
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.speech.speech_judge import SpeechJudge, SpeechLevel
from app.speech.speech_queue import SpeechQueue, SpeechItem, PRIORITY_S, PRIORITY_A, PRIORITY_B
from app.core.event_bus import Event, EventType


class FakeQueue:
    """模拟 SpeechQueue"""
    def __init__(self):
        self.items = []

    def enqueue(self, item: SpeechItem) -> bool:
        self.items.append(item)
        return True


class TestSpeechJudgeToQueue(unittest.TestCase):
    def setUp(self):
        self.queue = FakeQueue()
        self.judge = SpeechJudge({"intensity": "normal", "min_level": "B"}, queue=self.queue)

    def _make_event(self, event_type: str, data: dict = None) -> Event:
        return Event(
            event_type=event_type,
            data=data or {},
            source="test"
        )

    def test_1_s_event_to_queue(self):
        """S 级事件 → Queue"""
        event = self._make_event(EventType.BARON_KILLED, {"killer_name": "敌方"})
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 1)
        item = self.queue.items[0]
        self.assertEqual(item.priority, PRIORITY_S)

    def test_2_a_event_to_queue(self):
        """A 级事件 → Queue"""
        event = self._make_event(EventType.FIRST_BLOOD, {})
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 1)
        item = self.queue.items[0]
        self.assertEqual(item.priority, PRIORITY_A)

    def test_3_b_event_to_queue(self):
        """B 级事件 → Queue"""
        event = self._make_event(EventType.CHAMPION_KILL, {
            "killer_name": "敌方",
            "victim_name": "你",
            "is_current_player_death": True,
        })
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 1)
        item = self.queue.items[0]
        self.assertEqual(item.priority, PRIORITY_B)

    def test_4_drop_event_not_to_queue(self):
        """DROP 事件不进入 Queue"""
        event = self._make_event(EventType.GAME_TIME_UPDATE, {})
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 0)

    def test_5_text_generated(self):
        """文本正确生成"""
        event = self._make_event(EventType.BARON_KILLED, {"killer_name": "敌方"})
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 1)
        self.assertIn("男爵", self.queue.items[0].text)

    def test_6_category_correct(self):
        """category 正确"""
        # 目标事件
        event = self._make_event(EventType.DRAGON_KILLED, {"dragon_type": "fire"})
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 1)
        self.assertEqual(self.queue.items[0].category, "objective")

    def test_7_event_id_generated(self):
        """event_id 生成"""
        event = self._make_event(EventType.BARON_KILLED, {"killer_name": "敌方"})
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 1)
        self.assertTrue(len(self.queue.items[0].event_id) > 0)

    def test_8_different_events_different_ids(self):
        """不同事件有不同 event_id"""
        # 跳过频率限制
        self.judge.min_interval = 0

        event1 = self._make_event(EventType.BARON_KILLED, {"killer_name": "A"})
        event1.data["game_time"] = 100
        event1.timestamp = 1000000001.0
        self.judge._judge_and_speak(event1)

        event2 = self._make_event(EventType.DRAGON_KILLED, {"killer_name": "B"})
        event2.data["game_time"] = 200
        event2.timestamp = 1000000002.0
        self.judge._judge_and_speak(event2)

        self.assertEqual(len(self.queue.items), 2)
        self.assertNotEqual(self.queue.items[0].event_id, self.queue.items[1].event_id)

    def test_9_ttl_set(self):
        """TTL 正确设置"""
        # 用 FIRST_BLOOD（A 级，kill category，没有 category 覆盖）
        event = self._make_event(EventType.FIRST_BLOOD, {})
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 1)
        item = self.queue.items[0]
        # A 级 TTL 6 秒
        self.assertGreater(item.expire_at - time.time(), 5)
        self.assertLess(item.expire_at - time.time(), 7)

    def test_10_frequency_limit(self):
        """频率限制生效"""
        event = self._make_event(EventType.BARON_KILLED, {"killer_name": "敌方"})
        self.judge._judge_and_speak(event)

        # 立即再来一个
        self.judge._judge_and_speak(event)

        # 只有第一个进入了 Queue
        self.assertEqual(len(self.queue.items), 1)

    def test_11_alarm_event(self):
        """Alarm 事件进入 Queue"""
        alarm_data = {
            "type": "flash",
            "title": "闪现",
            "expire_at": time.time() + 30,
        }
        event = self._make_event(EventType.ALARM_TRIGGERED, {
            "alarm": alarm_data,
            "remaining": 5,
        })
        self.judge._judge_and_speak(event)

        self.assertEqual(len(self.queue.items), 1)
        self.assertEqual(self.queue.items[0].category, "alarm")

    def test_12_without_queue_publishes_event(self):
        """没有 Queue 时发布 SPEECH_SAY 事件"""
        bus = MagicMock()
        judge = SpeechJudge({"intensity": "normal", "min_level": "B"}, queue=None)
        judge.event_bus = bus

        event = self._make_event(EventType.BARON_KILLED, {"killer_name": "敌方"})
        judge._judge_and_speak(event)

        # 应该发布了 SPEECH_SAY 事件
        bus.publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
