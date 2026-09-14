"""
TTS 控制器测试
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.speech.tts import TTSController, TTSState


class TestTTSController(unittest.TestCase):
    """TTS 控制器测试"""

    def setUp(self):
        """每个测试前初始化"""
        self.config = {
            "engine": "edge",
            "edge_voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "volume": "+0%",
            "output_dir": "./data/tts_test",
            "mpv_path": r"D:\新建文件夹 (2)\mpv.exe",
            "enabled": True,
        }
        self.tts = TTSController(self.config)
        self.tts.start()

    def tearDown(self):
        """每个测试后清理"""
        self.tts.stop()
        time.sleep(0.5)

    def test_1_initial_state(self):
        """测试初始状态"""
        self.assertEqual(self.tts.get_state(), TTSState.IDLE)
        self.assertFalse(self.tts.is_playing())

    def test_2_empty_text(self):
        """测试空文本"""
        self.tts.speak("")
        time.sleep(0.2)
        self.assertFalse(self.tts.is_playing())

    def test_3_generate_and_play(self):
        """测试生成和播放"""
        self.tts.speak("测试语音")
        time.sleep(1)

        # 应该在 GENERATING 或 PLAYING 或已完成
        state = self.tts.get_state()
        self.assertIn(state, [TTSState.GENERATING, TTSState.PLAYING, TTSState.IDLE])

        # 等待完成
        self.tts.wait_finished(timeout=15)
        self.assertEqual(self.tts.get_state(), TTSState.IDLE)

    def test_4_is_playing(self):
        """测试 is_playing"""
        self.assertFalse(self.tts.is_playing())

        self.tts.speak("这是一个比较长的测试语音，用来验证播放状态检测。")
        time.sleep(1.5)

        # 应该正在播放或刚结束
        state = self.tts.get_state()
        self.assertIn(state, [TTSState.GENERATING, TTSState.PLAYING, TTSState.IDLE])

        self.tts.wait_finished(timeout=15)

    def test_5_stop_playback(self):
        """测试停止播放"""
        self.tts.speak("这是一个用于测试中断能力的较长语音。现在应该可以在播放过程中被停止。")
        time.sleep(2)

        # 确认正在播放
        if self.tts.is_playing():
            self.tts.stop_playback()
            time.sleep(0.5)
            self.assertEqual(self.tts.get_state(), TTSState.IDLE)
            self.assertFalse(self.tts.is_playing())

    def test_6_consecutive_speak(self):
        """测试连续播放"""
        self.tts.speak("第一句测试")
        time.sleep(0.5)

        # 第二句应该打断第一句
        self.tts.speak("第二句测试")
        time.sleep(0.5)

        # 等待完成
        self.tts.wait_finished(timeout=20)
        self.assertEqual(self.tts.get_state(), TTSState.IDLE)

    def test_7_stress_test(self):
        """压力测试：反复 speak/stop"""
        for i in range(5):
            self.tts.speak(f"压力测试第{i}句")
            time.sleep(0.5)
            self.tts.stop_playback()
            time.sleep(0.2)

        self.assertEqual(self.tts.get_state(), TTSState.IDLE)


if __name__ == "__main__":
    unittest.main()
