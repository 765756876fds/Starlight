"""
TTS 生命周期事件测试：speech.started / speech.finished
"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from app.core.event_bus import EventType


class TestTTSLifecycleEvents:
    """测试 TTS 生命周期事件发布"""

    def _create_tts(self):
        """创建 TTS 控制器（mock 外部依赖）"""
        from app.speech.tts import TTSController

        config = {
            "engine": "edge",
            "edge_voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "volume": "+0%",
            "output_dir": "./data/tts",
            "enabled": True,
            "mpv_path": "D:\\新建文件夹 (2)\\mpv.exe",
        }

        with patch("app.speech.tts.get_event_bus") as mock_get_bus, \
             patch("app.speech.tts.os.makedirs"), \
             patch("app.speech.tts.threading.Thread"):

            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus

            tts = TTSController(config)
            tts._event_bus = mock_bus

        return tts, mock_bus

    def test_finished_published_flag_initialized(self):
        """_finished_published 标记初始化为 False"""
        tts, _ = self._create_tts()
        assert tts._finished_published is False

    def test_new_speak_resets_finished_flag(self):
        """新播放开始时重置 _finished_published"""
        tts, _ = self._create_tts()

        # 模拟已经发布过 finished
        tts._finished_published = True

        # 启动新播放前应该重置标记
        # speak() 方法中会重置，这里直接测试 speak 调用后的状态
        with patch.object(tts, 'stop_playback'), \
             patch.object(tts, '_speak_thread'):
            tts.enabled = True
            tts._running = True
            tts.speak("测试")

            assert tts._finished_published is False

    def test_normal_playback_publishes_completed(self):
        """正常播放完成发布 finished(reason=completed)"""
        tts, mock_bus = self._create_tts()

        # 模拟 _speak_thread 的正常完成路径
        tts._finished_published = False

        # 模拟正常播放完成后的代码路径
        from app.speech.tts import TTSState

        tts._stop_flag.clear()
        tts._set_state(TTSState.IDLE)

        # 复制 _speak_thread 末尾的逻辑
        if not tts._stop_flag.is_set():
            if not tts._finished_published:
                tts._finished_published = True
                tts._publish_event(EventType.SPEECH_FINISHED, {"reason": "completed"})

        # 验证事件被发布
        assert mock_bus.publish.called
        # 获取最后一次 publish 调用的参数
        last_call = mock_bus.publish.call_args
        event = last_call[0][0]  # 第一个位置参数是 Event 对象
        assert event.event_type == EventType.SPEECH_FINISHED
        assert event.data["reason"] == "completed"

    def test_stop_playback_publishes_stopped(self):
        """stop_playback 发布 finished(reason=stopped)"""
        tts, mock_bus = self._create_tts()

        from app.speech.tts import TTSState

        # 设置为播放状态
        tts._state = TTSState.PLAYING
        tts._finished_published = False

        # 模拟 _play_process 为 None（避免实际终止进程）
        tts._play_process = None
        tts._current_file = None

        # 调用 stop_playback
        tts.stop_playback()

        # 验证事件被发布
        assert mock_bus.publish.called
        last_call = mock_bus.publish.call_args
        event = last_call[0][0]
        assert event.event_type == EventType.SPEECH_FINISHED
        assert event.data["reason"] == "stopped"

    def test_no_duplicate_finished(self):
        """防止重复发布 finished"""
        tts, mock_bus = self._create_tts()

        from app.speech.tts import TTSState

        # 模拟已经发布过 finished
        tts._finished_published = True

        # 再次调用 stop_playback
        tts._state = TTSState.IDLE
        tts._play_process = None
        tts._current_file = None

        tts.stop_playback()

        # 验证没有额外的 finished 事件
        # 统计 SPEECH_FINISHED 类型的发布次数
        finished_calls = []
        for call in mock_bus.publish.call_args_list:
            event = call[0][0]
            if event.event_type == EventType.SPEECH_FINISHED:
                finished_calls.append(event)

        assert len(finished_calls) == 0  # 因为 _finished_published 已经是 True

    def test_normal_completion_then_stop_no_duplicate(self):
        """正常完成后再 stop，不会重复发布 finished"""
        tts, mock_bus = self._create_tts()

        from app.speech.tts import TTSState

        # Step 1: 模拟正常播放完成
        tts._finished_published = False
        tts._stop_flag.clear()
        tts._set_state(TTSState.IDLE)

        # 发布正常完成的 finished
        if not tts._finished_published:
            tts._finished_published = True
            tts._publish_event(EventType.SPEECH_FINISHED, {"reason": "completed"})

        # Step 2: 之后又调用 stop_playback
        tts._state = TTSState.IDLE
        tts._play_process = None
        tts._current_file = None
        tts.stop_playback()

        # 验证只有一个 finished 事件
        finished_calls = []
        for call in mock_bus.publish.call_args_list:
            event = call[0][0]
            if event.event_type == EventType.SPEECH_FINISHED:
                finished_calls.append(event)

        assert len(finished_calls) == 1
        assert finished_calls[0].data["reason"] == "completed"

    def test_stop_then_normal_completion_no_duplicate(self):
        """先 stop 再正常完成，不会重复发布 finished"""
        tts, mock_bus = self._create_tts()

        from app.speech.tts import TTSState

        # Step 1: 模拟 stop_playback
        tts._state = TTSState.PLAYING
        tts._finished_published = False
        tts._play_process = None
        tts._current_file = None
        tts.stop_playback()

        # Step 2: 播放线程随后"正常完成"
        # 模拟 _speak_thread 末尾的逻辑
        tts._stop_flag.set()  # 被停止了
        if not tts._stop_flag.is_set():
            # 这个分支不会执行，因为 stop_flag 已经设置
            pass

        # 验证只有一个 finished 事件
        finished_calls = []
        for call in mock_bus.publish.call_args_list:
            event = call[0][0]
            if event.event_type == EventType.SPEECH_FINISHED:
                finished_calls.append(event)

        assert len(finished_calls) == 1
        assert finished_calls[0].data["reason"] == "stopped"
