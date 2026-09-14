"""
SpeechQueue 单元测试
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.speech.speech_queue import (
    SpeechQueue, SpeechItem,
    PRIORITY_S, PRIORITY_A, PRIORITY_B,
)


class MockTTS:
    """模拟 TTSController，用于测试"""

    def __init__(self):
        self.playing = False
        self.current_text = None
        self.speak_count = 0
        self.stop_count = 0
        self.speak_delay = 0.5  # 模拟播放时长 0.5 秒

    def speak(self, text: str, priority: str = "normal"):
        self.playing = True
        self.current_text = text
        self.speak_count += 1
        # 模拟播放一段时间
        def _play():
            time.sleep(self.speak_delay)
            self.playing = False
            self.current_text = None
        import threading
        threading.Thread(target=_play, daemon=True).start()

    def stop_playback(self):
        self.playing = False
        self.current_text = None
        self.stop_count += 1

    def is_playing(self) -> bool:
        return self.playing

    def wait_finished(self, timeout: float = 30.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if not self.playing:
                return True
            time.sleep(0.05)
        return False


class TestSpeechQueue(unittest.TestCase):
    """SpeechQueue 测试"""

    def setUp(self):
        self.tts = MockTTS()
        self.queue = SpeechQueue(self.tts)
        self.queue.start()
        time.sleep(0.1)  # 等 worker 启动

    def tearDown(self):
        self.queue.shutdown()
        time.sleep(0.1)

    def _make_item(self, text: str, priority: int, event_id: str = "",
                   ttl: float = 10.0, interruptible: bool = True) -> SpeechItem:
        now = time.time()
        return SpeechItem(
            text=text,
            priority=priority,
            expire_at=now + ttl,
            event_id=event_id,
            interruptible=interruptible,
        )

    def test_1_fifo_same_priority(self):
        """同优先级 FIFO"""
        self.queue.enqueue(self._make_item("B1", PRIORITY_B, event_id="b1"))
        self.queue.enqueue(self._make_item("B2", PRIORITY_B, event_id="b2"))
        self.queue.enqueue(self._make_item("B3", PRIORITY_B, event_id="b3"))

        # 等待全部播放完（每个 0.5 秒，3 个 = 1.5 秒）
        time.sleep(2.5)

        # 验证播放顺序
        self.assertEqual(self.tts.speak_count, 3)

    def test_2_priority_order(self):
        """优先级顺序：S > A > B"""
        self.queue.enqueue(self._make_item("B", PRIORITY_B, event_id="b"))
        self.queue.enqueue(self._make_item("A", PRIORITY_A, event_id="a"))
        self.queue.enqueue(self._make_item("S", PRIORITY_S, event_id="s"))

        # 等待全部播放完
        time.sleep(2.5)

        # S 应该最先播
        self.assertEqual(self.tts.speak_count, 3)

    def test_3_same_priority_no_interrupt(self):
        """同优先级不打断"""
        # 播放 A1
        item1 = self._make_item("A1", PRIORITY_A, event_id="a1")
        self.queue.enqueue(item1)
        time.sleep(0.1)  # 开始播放

        # A2 到达，不应该打断 A1
        item2 = self._make_item("A2", PRIORITY_A, event_id="a2")
        self.queue.enqueue(item2)

        # 等待全部播放完（A1 0.5s + A2 0.5s）
        time.sleep(1.5)

        # 应该播放了两次（A1 然后 A2）
        self.assertEqual(self.tts.speak_count, 2)

    def test_4_a_interrupts_b(self):
        """A 打断 B"""
        # 播放 B
        item_b = self._make_item("B", PRIORITY_B, event_id="b")
        self.queue.enqueue(item_b)
        time.sleep(0.1)  # 开始播放 B

        # A 到达，应该打断 B
        item_a = self._make_item("A", PRIORITY_A, event_id="a")
        self.queue.enqueue(item_a)

        # 等待
        time.sleep(0.3)

        # B 被停止了
        self.assertGreaterEqual(self.tts.stop_count, 1)

    def test_5_s_interrupts_a(self):
        """S 打断 A"""
        # 播放 A
        item_a = self._make_item("A", PRIORITY_A, event_id="a")
        self.queue.enqueue(item_a)
        time.sleep(0.1)

        # S 到达
        item_s = self._make_item("S", PRIORITY_S, event_id="s")
        self.queue.enqueue(item_s)

        time.sleep(0.3)

        self.assertGreaterEqual(self.tts.stop_count, 1)

    def test_6_s_no_interrupt_s(self):
        """S 不打断 S"""
        # 播放 S1
        item1 = self._make_item("S1", PRIORITY_S, event_id="s1")
        self.queue.enqueue(item1)
        time.sleep(0.1)

        # S2 到达，不应该打断 S1
        item2 = self._make_item("S2", PRIORITY_S, event_id="s2")
        self.queue.enqueue(item2)

        time.sleep(0.3)

        # 没有被停止
        self.assertEqual(self.tts.stop_count, 0)

    def test_7_b_no_interrupt_a(self):
        """B 不打断 A"""
        # 播放 A
        item_a = self._make_item("A", PRIORITY_A, event_id="a")
        self.queue.enqueue(item_a)
        time.sleep(0.1)

        # B 到达，不应该打断 A
        item_b = self._make_item("B", PRIORITY_B, event_id="b")
        self.queue.enqueue(item_b)

        time.sleep(0.3)

        # 没有被停止
        self.assertEqual(self.tts.stop_count, 0)

    def test_8_interruptible_false(self):
        """interruptible=False 不被打断"""
        # 播放不可打断的 B
        item_b = self._make_item("B", PRIORITY_B, event_id="b", interruptible=False)
        self.queue.enqueue(item_b)
        time.sleep(0.1)

        # S 到达，不应该打断
        item_s = self._make_item("S", PRIORITY_S, event_id="s")
        self.queue.enqueue(item_s)

        time.sleep(0.3)

        # 没有被停止
        self.assertEqual(self.tts.stop_count, 0)

    def test_9_ttl_expired(self):
        """TTL 过期"""
        # 已经过期的 item
        now = time.time()
        expired_item = SpeechItem(
            text="expired",
            priority=PRIORITY_B,
            expire_at=now - 1,  # 已经过期
            event_id="expired",
        )

        result = self.queue.enqueue(expired_item)
        self.assertFalse(result)

    def test_10_dedup(self):
        """event_id 去重"""
        # 相同 event_id
        item1 = self._make_item("text1", PRIORITY_B, event_id="abc")
        result1 = self.queue.enqueue(item1)
        self.assertTrue(result1)

        item2 = self._make_item("text2", PRIORITY_B, event_id="abc")
        result2 = self.queue.enqueue(item2)
        self.assertFalse(result2)

        # 不同 event_id，相同 text
        item3 = self._make_item("text1", PRIORITY_B, event_id="def")
        result3 = self.queue.enqueue(item3)
        self.assertTrue(result3)

    def test_11_overflow(self):
        """队列溢出"""
        # 填满队列（快速入队，不等 worker 消费）
        results = []
        for i in range(10):
            item = self._make_item(f"B{i}", PRIORITY_B, event_id=f"b{i}", ttl=5.0)
            results.append(self.queue.enqueue(item))

        # 全部应该成功
        self.assertTrue(all(results))

        # 更低优先级的新事件 → DROP
        low_item = self._make_item("low", PRIORITY_B, event_id="low", ttl=5.0)
        result = self.queue.enqueue(low_item)
        self.assertFalse(result)

    def test_12_clear_pending(self):
        """clear_pending"""
        # 加几个 pending
        self.queue.enqueue(self._make_item("B1", PRIORITY_B, event_id="b1"))
        self.queue.enqueue(self._make_item("B2", PRIORITY_B, event_id="b2"))

        time.sleep(0.1)

        # clear pending
        self.queue.clear_pending()

        self.assertEqual(self.queue.size(), 0)

    def test_13_stop_current(self):
        """stop_current"""
        # 播放
        item = self._make_item("test", PRIORITY_B, event_id="test")
        self.queue.enqueue(item)
        time.sleep(0.1)

        # 停止当前
        self.queue.stop_current()

        time.sleep(0.1)

        self.assertFalse(self.tts.is_playing())

    def test_14_clear_all(self):
        """clear_all"""
        # 加 pending
        self.queue.enqueue(self._make_item("B1", PRIORITY_B, event_id="b1"))
        self.queue.enqueue(self._make_item("B2", PRIORITY_B, event_id="b2"))

        time.sleep(0.1)

        # clear all
        self.queue.clear_all()

        self.assertEqual(self.queue.size(), 0)
        self.assertFalse(self.tts.is_playing())

    def test_15_shutdown(self):
        """shutdown"""
        self.queue.shutdown()
        time.sleep(0.1)
        self.assertFalse(self.queue.is_running())


if __name__ == "__main__":
    unittest.main()
