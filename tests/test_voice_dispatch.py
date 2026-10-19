"""
VoiceRuntime command dispatch wiring 测试
"""
from unittest.mock import MagicMock

from app.voice.runtime import VoiceRuntime
from app.voice.intent import Intent
from app.core.command_bus import CommandType


class TestVoiceRuntimeDispatch:
    """验证 VoiceRuntime._execute_intent 正确分发 MUSIC 命令到 CommandBus"""

    def _make_runtime(self):
        rt = VoiceRuntime.__new__(VoiceRuntime)
        rt.command_bus = MagicMock()
        rt.event_bus = MagicMock()
        # system.stop_tts 独立路由所需的惰性提供者（默认为空，不崩）
        rt.tts_provider = None
        rt.speech_queue_provider = None
        return rt

    def test_highlight_mode_on(self):
        rt = self._make_runtime()
        intent = Intent(CommandType.MUSIC_HIGHLIGHT_MODE, confidence=0.99,
                        params={"enabled": True})
        rt._execute_intent(intent, "打开高潮模式")
        rt.command_bus.execute.assert_called_once()
        cmd = rt.command_bus.execute.call_args[0][0]
        assert cmd.command_type == CommandType.MUSIC_HIGHLIGHT_MODE
        assert cmd.params["enabled"] is True

    def test_highlight_mode_off(self):
        rt = self._make_runtime()
        intent = Intent(CommandType.MUSIC_HIGHLIGHT_MODE, confidence=0.99,
                        params={"enabled": False})
        rt._execute_intent(intent, "关闭高潮模式")
        cmd = rt.command_bus.execute.call_args[0][0]
        assert cmd.params["enabled"] is False

    def test_set_play_mode_sequential(self):
        rt = self._make_runtime()
        intent = Intent(CommandType.MUSIC_SET_PLAY_MODE, confidence=0.99,
                        params={"mode": "sequential"})
        rt._execute_intent(intent, "连续播放")
        cmd = rt.command_bus.execute.call_args[0][0]
        assert cmd.command_type == CommandType.MUSIC_SET_PLAY_MODE
        assert cmd.params["mode"] == "sequential"

    def test_set_play_mode_shuffle(self):
        rt = self._make_runtime()
        intent = Intent(CommandType.MUSIC_SET_PLAY_MODE, confidence=0.99,
                        params={"mode": "shuffle"})
        rt._execute_intent(intent, "随机播放")
        cmd = rt.command_bus.execute.call_args[0][0]
        assert cmd.params["mode"] == "shuffle"

    def test_set_play_mode_repeat_one(self):
        rt = self._make_runtime()
        intent = Intent(CommandType.MUSIC_SET_PLAY_MODE, confidence=0.99,
                        params={"mode": "repeat_one"})
        rt._execute_intent(intent, "单曲循环")
        cmd = rt.command_bus.execute.call_args[0][0]
        assert cmd.params["mode"] == "repeat_one"

    # ===== system.stop_tts 独立打断路由（不走 CommandBus）=====

    def test_stop_tts_calls_providers_directly(self):
        """system.stop_tts：直接调 TTS stop_playback + SpeechQueue stop_current，不碰 CommandBus。"""
        rt = self._make_runtime()
        tts = MagicMock()
        sq = MagicMock()
        rt.tts_provider = lambda: tts
        rt.speech_queue_provider = lambda: sq
        intent = Intent("system.stop_tts", confidence=0.99)
        rt._execute_intent(intent, "停止播放")
        tts.stop_playback.assert_called_once()
        sq.stop_current.assert_called_once()
        rt.command_bus.execute.assert_not_called()
        rt.command_bus.execute_async.assert_not_called()

    def test_stop_tts_provider_none_no_crash(self):
        """提供者为空（未装配）时：降级 no-op，不抛异常不卡链。"""
        rt = self._make_runtime()
        intent = Intent("system.stop_tts", confidence=0.99)
        rt._execute_intent(intent, "闭嘴")
        rt.command_bus.execute.assert_not_called()

    def test_stop_tts_provider_exception_no_crash(self):
        """提供者抛异常：只打日志，不影响识别线程。"""
        rt = self._make_runtime()

        def boom_tts():
            raise RuntimeError("tts down")
        rt.tts_provider = boom_tts

        def boom_sq():
            raise RuntimeError("queue down")
        rt.speech_queue_provider = boom_sq
        intent = Intent("system.stop_tts", confidence=0.99)
        rt._execute_intent(intent, "别说了")
        rt.command_bus.execute.assert_not_called()
