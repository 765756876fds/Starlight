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
