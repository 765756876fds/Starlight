"""
Phase 2.4 集成测试：2999 → StateDiff → EventBus → SpeechJudge → SpeechQueue
"""
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
        """Test 1：ChampionKill 完整链路"""
        # 发布一个 ChampionKill 事件
        event = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "敌方英雄",
                "victim_name": "你",
                "is_current_player_death": True,
                "game_time": 100.0,
            },
            source="test"
        )
        self.bus.publish(event)

        # 等待 SpeechJudge 处理
        time.sleep(0.2)

        # Queue 应该有一个 SpeechItem
        self.assertGreaterEqual(self.queue.size() + (1 if self.queue.get_current() else 0), 1)

    def test_2_same_snapshot_no_duplicate(self):
        """Test 2：相同事件不重复播报"""
        self.judge.min_interval = 0  # 跳过频率限制

        # 第一次发布
        event1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "A",
                "victim_name": "B",
                "game_time": 100.0,
            },
            source="test"
        )
        self.bus.publish(event1)
        time.sleep(0.1)

        # 第二次发布完全相同的事件
        event2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "A",
                "victim_name": "B",
                "game_time": 100.0,
            },
            source="test"
        )
        self.bus.publish(event2)
        time.sleep(0.1)

        # TTS 只应该 speak 一次（Queue dedup）
        # 注意：因为 Queue 有 dedup，相同 event_id 第二次会被 DROP
        self.assertEqual(self.tts.speak_count, 1)

    def test_3_different_kills(self):
        """Test 3：两个不同的击杀事件"""
        self.judge.min_interval = 0

        # Kill A（当前玩家击杀）
        event1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "你",
                "victim_name": "敌方1",
                "is_current_player_kill": True,
                "game_time": 100.0,
            },
            source="test"
        )
        id1 = self.judge._get_event_id(event1)
        print(f"DEBUG: event1 id = {id1}")
        self.bus.publish(event1)
        time.sleep(0.5)  # 等第一个播完

        # Kill B（当前玩家又击杀了）
        event2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "你",
                "victim_name": "敌方2",
                "is_current_player_kill": True,
                "game_time": 200.0,
            },
            source="test"
        )
        id2 = self.judge._get_event_id(event2)
        print(f"DEBUG: event2 id = {id2}")
        self.bus.publish(event2)
        time.sleep(0.5)

        print(f"DEBUG: after wait, speak_count = {self.tts.speak_count}, queue size = {self.queue.size()}")

        # 应该 speak 两次
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
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "敌方",
                "victim_name": "你",
                "is_current_player_death": True,
                "game_time": 100.0,
            },
            source="test"
        )
        self.bus.publish(event)
        time.sleep(0.2)

        # 检查 TTS 是否收到了正确的文本
        self.assertEqual(self.tts.speak_count, 1)
        self.assertIn("击杀", self.tts.current_text or "")


if __name__ == "__main__":
    unittest.main()
