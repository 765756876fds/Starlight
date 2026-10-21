"""
TTS 控制器测试
"""
import pytest

pytestmark = pytest.mark.unit

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.speech.tts import TTSController, TTSState


class FakeCommunicate:
    """离线 fake：替代 edge_tts.Communicate（写一个 >1000 字节的假音频）"""

    def __init__(self, **kwargs):
        pass

    async def save(self, path: str):
        with open(path, "wb") as f:
            f.write(b"\x00" * 2048)


class TestTTSController(unittest.TestCase):
    """TTS 控制器测试"""

    def setUp(self):
        self.config = {
            "engine": "edge",
            "edge_voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "volume": "+0%",
            "output_dir": "./data/tts_test",
            "mpv_path": r"D:\新建文件夹 (2)\mpv.exe",
            "enabled": True,
        }
        # 离线化：mock Edge TTS（避免真实网络 403 依赖）
        self._edge_patcher = mock.patch("edge_tts.Communicate", FakeCommunicate)
        self._edge_patcher.start()
        self.tts = TTSController(self.config)
        self.tts.start()

    def tearDown(self):
        """每个测试后清理"""
        self.tts.stop()
        time.sleep(0.5)
        self._edge_patcher.stop()

    def _wait_state(self, state, timeout=3.0):
        """轮询等待 TTS 进入指定状态。

        stop_playback 已异步化：锁内置 STOPPING 后瞬间返回，
        真正的进程终止/清理/收尾在后台线程（tts-stop）完成 →
        测试用轮询代替同步断言。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.tts.get_state() == state:
                return
            time.sleep(0.05)
        self.assertEqual(self.tts.get_state(), state)

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
            self._wait_state(TTSState.IDLE)
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

        self._wait_state(TTSState.IDLE)

    def _qwen_config(self, **overrides):
        cfg = {
            "engine": "qwen",
            "output_dir": "./data/tts_test",
            "qwen_api_key": "sk-test",
            "qwen_base_url": "https://dashscope.aliyuncs.com/api/v1",
            "qwen_model": "qwen-audio-3.1-tts-flash",
            "qwen_voice": "longanlingxin_v3.1",
        }
        cfg.update(overrides)
        return cfg

    def test_qwen_generate_success(self):
        """Qwen 引擎：合成成功返回 wav 文件"""
        tts = TTSController(self._qwen_config())
        with mock.patch("requests.post") as m_post, mock.patch("requests.get") as m_get:
            m_post.return_value.status_code = 200
            m_post.return_value.json.return_value = {
                "output": {"audio": {"url": "http://fake-oss/x.wav"}}
            }
            m_get.return_value.raise_for_status.return_value = None
            m_get.return_value.content = b"\x00" * 4096
            out = tts._generate_audio("测试")
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith(".wav"))
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 1000)
        if out and os.path.exists(out):
            os.remove(out)

    def test_qwen_fail_fallback_edge(self):
        """Qwen 失败 → 回退 Edge 引擎（保底）"""
        tts = TTSController(self._qwen_config())
        with mock.patch("requests.post", side_effect=RuntimeError("boom")):
            out = tts._generate_audio("测试")
        # Qwen 失败 → 回退 edge（FakeCommunicate 成功写假 mp3）
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith(".mp3"))
        if out and os.path.exists(out):
            os.remove(out)

    def test_qwen_missing_key(self):
        """Qwen 无 Key → 不发起 Qwen 请求（自动回退 Edge）"""
        with mock.patch.object(TTSController, "_resolve_qwen_key", return_value=""):
            tts = TTSController(self._qwen_config(qwen_api_key=""))
            with mock.patch("requests.post") as m_post:
                out = tts._generate_audio("测试")
        m_post.assert_not_called()              # Qwen 请求从未发出
        self.assertTrue(out.endswith(".mp3"))   # 回退 Edge（FakeCommunicate）成功
        if out and os.path.exists(out):
            os.remove(out)

    def test_qwen_body_has_rate_volume(self):
        """Qwen 引擎：请求 body 含 rate（0.5~2.0 float）与 volume（0~100 int）"""
        tts = TTSController(self._qwen_config(qwen_rate=1.5, qwen_volume=80))
        with mock.patch("requests.post") as m_post, mock.patch("requests.get") as m_get:
            m_post.return_value.status_code = 200
            m_post.return_value.json.return_value = {
                "output": {"audio": {"url": "http://fake-oss/x.wav"}}
            }
            m_get.return_value.raise_for_status.return_value = None
            m_get.return_value.content = b"\x00" * 4096
            out = tts._generate_audio("测试")
        self.assertIsNotNone(out)
        body = m_post.call_args.kwargs["json"]
        self.assertEqual(body["input"]["rate"], 1.5)
        self.assertEqual(body["input"]["volume"], 80)
        if out and os.path.exists(out):
            os.remove(out)

    def test_qwen_setters_route_by_engine(self):
        """set_voice/set_rate/set_volume 按引擎路由（qwen 改 qwen_* 字段）"""
        tts = TTSController(self._qwen_config())
        tts.set_voice("longanhuan_v3.1")
        tts.set_rate(1.2)
        tts.set_volume(60)
        self.assertEqual(tts.qwen_voice, "longanhuan_v3.1")
        self.assertEqual(tts.qwen_rate, 1.2)
        self.assertEqual(tts.qwen_volume, 60)
        # Edge 字段不受影响
        self.assertEqual(tts.voice, "zh-CN-XiaoxiaoNeural")

    def test_has_qwen_key(self):
        """has_qwen_key() 公开方法：config 有 Key → True；无 Key 且文件缺失 → False"""
        tts = TTSController(self._qwen_config())
        self.assertTrue(tts.has_qwen_key())          # config 里 sk-test
        tts2 = TTSController(self._qwen_config(qwen_api_key=""))
        with mock.patch.object(TTSController, "_resolve_qwen_key", return_value=""):
            self.assertFalse(tts2.has_qwen_key())    # 三级全空 → False


if __name__ == "__main__":
    unittest.main()
