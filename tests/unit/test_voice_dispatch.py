"""
VoiceRuntime command dispatch wiring 测试
"""
import pytest

pytestmark = pytest.mark.unit

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

    def test_speed_without_parameter_prompts_without_dispatch(self):
        rt = self._make_runtime()
        rt._enqueue_speak = MagicMock()

        rt._execute_intent(Intent(CommandType.MUSIC_SET_SPEED, params={}), "倍速播放")

        rt.command_bus.execute.assert_not_called()
        rt._enqueue_speak.assert_called_once_with(
            "请说出要设置的倍速，例如一点五倍", "voice_music_speed")

    def test_speed_dispatches_once_and_reports_execution_failure(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = False
        rt._enqueue_speak = MagicMock()

        rt._execute_intent(
            Intent(CommandType.MUSIC_SET_SPEED, params={"speed": 1.5}),
            "倍速播放一点五倍",
        )

        rt.command_bus.execute.assert_called_once()
        cmd = rt.command_bus.execute.call_args[0][0]
        assert cmd.params == {"speed": 1.5}
        rt._enqueue_speak.assert_called_once_with(
            "倍速设置未成功，请检查播放器状态", "voice")

    def test_speed_execution_exception_reports_failure_once(self):
        rt = self._make_runtime()
        rt.command_bus.execute.side_effect = RuntimeError("player unavailable")
        rt._enqueue_speak = MagicMock()

        rt._execute_intent(
            Intent(CommandType.MUSIC_SET_SPEED, params={"speed": 1.5}),
            "设置一点五倍速",
        )

        rt.command_bus.execute.assert_called_once()
        rt._enqueue_speak.assert_called_once_with(
            "倍速设置未成功，请检查播放器状态", "voice")

    def test_automation_feedback_distinguishes_disconnected_client(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = {
            "ok": True, "enabled": True,
            "client_ready": False, "running": True,
        }
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(
            Intent(CommandType.AUTO_QUEUE_SET, params={"enabled": True}),
            "打开自动排队",
        )
        cmd = rt.command_bus.execute.call_args[0][0]
        assert cmd.params == {"enabled": True}
        rt._enqueue_speak.assert_called_once_with(
            "自动排队已开启，等待游戏连接", "voice_automation")

    def test_automation_failure_does_not_report_enabled(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = False
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(
            Intent(CommandType.AUTO_COMBO_SET, params={"enabled": True}),
            "打开自动连招",
        )
        rt._enqueue_speak.assert_called_once_with(
            "自动化操作未成功", "voice_automation")

    def test_cooldown_start_reports_failure(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = False
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent("cooldown.start", params={
            "champion": "石头人", "skill": "闪现"}), "石头人用了闪现")
        rt._enqueue_speak.assert_called_once_with(
            "技能记录未成功", "voice_cooldown")

    @pytest.mark.parametrize(("feature", "enabled", "message"), [
        ("queue", True, "自动排队已开启"),
        ("accept", False, "自动同意已关闭"),
        ("banpick", True, "自动禁用和选择英雄已开启"),
        ("combo", False, "自动连招已关闭"),
    ])
    def test_automation_status_query_reports_live_result(self, feature, enabled, message):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = {"available": True, "enabled": enabled}
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent(CommandType.AUTO_STATUS_QUERY,
                                  params={"feature": feature}), "查询自动化状态")
        rt._enqueue_speak.assert_called_once_with(message, "voice_automation_status")

    def test_automation_status_query_reports_unavailable(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = {"available": False}
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent(CommandType.AUTO_STATUS_QUERY,
                                  params={"feature": "combo"}), "查询自动连招状态")
        rt._enqueue_speak.assert_called_once_with(
            "暂时无法确认自动连招状态", "voice_automation_status")

    def test_combo_status_reports_listener_unavailable(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = {
            "available": True, "enabled": True, "listener_ready": False,
        }
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent(CommandType.AUTO_STATUS_QUERY,
                                  params={"feature": "combo"}), "查询自动连招状态")
        rt._enqueue_speak.assert_called_once_with(
            "自动连招已开启，但键盘监听器不可用", "voice_automation_status")

    def test_unknown_command_feedback_is_throttled(self):
        rt = self._make_runtime()
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent("system.unknown"), "这是一条无法识别的指令")
        rt._execute_intent(Intent("system.unknown"), "这是一条无法识别的指令")
        rt._enqueue_speak.assert_called_once_with(
            "没有听懂这条指令，请说得更具体一些", "voice_unknown")

    def test_empty_unknown_input_is_silent(self):
        rt = self._make_runtime()
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent("system.unknown"), "")
        rt._enqueue_speak.assert_not_called()

    def test_movie_stop_success_reports_once(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = True
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent(CommandType.MOVIE_STOP), "停止电影")
        rt.command_bus.execute.assert_called_once()
        rt._enqueue_speak.assert_called_once_with("电影已停止", "voice_movie_control")

    def test_movie_stop_failure_reports_without_retry(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = False
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent(CommandType.MOVIE_STOP), "停止电影")
        rt.command_bus.execute.assert_called_once()
        rt._enqueue_speak.assert_called_once_with("电影操作未成功", "voice_movie_control")

    def test_cooldown_cancel_feedback(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = True
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent("cooldown.cancel", params={"champion": "石头人"}), "取消石头人计时")
        rt._enqueue_speak.assert_called_once_with("技能计时已取消", "voice_cooldown")

    def test_cooldown_cancel_all_feedback(self):
        rt = self._make_runtime()
        rt.command_bus.execute.return_value = 2
        rt._enqueue_speak = MagicMock()
        rt._execute_intent(Intent("cooldown.cancel_all", params={}), "取消所有计时")
        rt._enqueue_speak.assert_called_once_with("已取消全部技能计时，共2个", "voice_cooldown")

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
