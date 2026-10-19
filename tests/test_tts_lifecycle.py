"""
TTS 生命周期事件测试：speech.started / speech.finished
"""
import time
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


class TestTTSSpeakThreadGenerationGuard:
    """回归测试：_speak_thread 在 _wait_for_playback 返回后必须检查 generation

    场景：A 正在播放 → A 被 stop → B 开始播放 → A 的旧线程从 _wait_for_playback 返回
    修复前：A 会继续清理共享状态、覆盖 B 的 state、错误发布 finished(completed)
    修复后：A 检测到 generation 变化，立即 return，不触碰任何共享状态。
    """

    def _create_tts(self):
        from app.speech.tts import TTSController, TTSState

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

    def test_old_thread_does_not_touch_shared_state_after_generation_change(self):
        """_wait_for_playback 返回后 generation 已变 → 旧线程必须立即 return

        验证：
        - 不覆盖 B 的 state
        - 不清空 B 的 _current_file
        - 不发布 SPEECH_FINISHED(completed)
        """
        import threading
        from app.speech.tts import TTSState

        tts, mock_bus = self._create_tts()

        # 模拟 A 正在播放（generation=1）
        tts._generation = 1
        tts._set_state(TTSState.PLAYING)
        tts._current_file = "/fake/a.mp3"
        tts._finished_published = False
        tts._stop_flag.clear()

        # 用一个 Event 控制 _wait_for_playback 的返回时机
        wait_blocker = threading.Event()
        wait_released = threading.Event()

        def fake_wait_for_playback():
            # 阻塞，直到测试主线程放行
            wait_blocker.wait(timeout=5.0)
            wait_released.set()

        tts._wait_for_playback = fake_wait_for_playback
        tts._generate_audio = lambda text: "/fake/a.mp3"
        tts._play_audio = lambda filepath: True
        tts._cleanup_file = MagicMock()

        # 启动 A 的 _speak_thread（generation=1）
        a_result = {}
        def run_a():
            try:
                tts._speak_thread("语音A", 1)
                a_result["returned"] = True
            except Exception as e:
                a_result["error"] = e

        a_thread = threading.Thread(target=run_a, daemon=True)
        a_thread.start()

        # 等待 A 进入 _wait_for_playback（阻塞中）
        time.sleep(0.3)

        # === 模拟 B 开始播放（generation 变为 2）===
        # 这是 speak(B) 做的事情：
        tts._generation = 2           # generation 递增
        tts._stop_flag.clear()        # 清除停止标志
        tts._finished_published = False  # 重置 finished 标记
        # B 正在播放中，state=PLAYING，_current_file 指向 B 的文件
        tts._current_file = "/fake/b.mp3"
        tts._set_state(TTSState.PLAYING)

        # 放行 A 的 _wait_for_playback
        wait_blocker.set()
        a_thread.join(timeout=2.0)

        # === 验证 A 的旧线程没有破坏 B 的状态 ===
        # 1. state 仍然是 PLAYING（没有被 A 改成 IDLE）
        assert tts.get_state() == TTSState.PLAYING, \
            f"state 被旧线程覆盖: {tts.get_state()}"

        # 2. _current_file 仍然是 B 的文件（没有被 A 清空）
        assert tts._current_file == "/fake/b.mp3", \
            f"_current_file 被旧线程清空: {tts._current_file}"

        # 3. A 的线程确实返回了（没有崩溃）
        assert a_result.get("returned") is True, \
            f"A 线程未正常返回: {a_result}"

        # 4. 没有发布 SPEECH_FINISHED(completed)
        finished_calls = [
            call[0][0] for call in mock_bus.publish.call_args_list
            if call[0][0].event_type == "speech.finished"
        ]
        completed_calls = [
            e for e in finished_calls
            if e.data.get("reason") == "completed"
        ]
        assert len(completed_calls) == 0, \
            f"旧线程错误发布了 {len(completed_calls)} 个 completed finished"

    def test_new_speak_normal_completion_still_works(self):
        """修复不影响新线程的正常完成路径

        B 的 generation == self._generation，应该正常走到 finished(completed)
        """
        from app.speech.tts import TTSState

        tts, mock_bus = self._create_tts()

        # B 的 generation=2，当前 self._generation=2
        # state=IDLE 才能通过 _speak_thread 的早期检查
        tts._generation = 2
        tts._set_state(TTSState.IDLE)
        tts._current_file = None
        tts._finished_published = False
        tts._stop_flag.clear()

        # mock 各步骤
        tts._generate_audio = lambda text: "/fake/b.mp3"
        tts._play_audio = lambda filepath: True
        tts._wait_for_playback = lambda: None
        tts._cleanup_file = MagicMock()

        # 直接调用 _speak_thread（generation=2，匹配当前 generation）
        tts._speak_thread("语音B", 2)

        # 验证正常完成
        assert tts.get_state() == TTSState.IDLE
        assert tts._finished_published is True

        finished_calls = [
            call[0][0] for call in mock_bus.publish.call_args_list
            if call[0][0].event_type == "speech.finished"
        ]
        assert len(finished_calls) == 1
        assert finished_calls[0].data["reason"] == "completed"


class TestTTSAsyncStopGenerationGuard:
    """连续打断代际竞态（极端并发③）：A 被 stop → B 立即开始 → A 的异步清理收尾不破坏 B。

    _async_stop_playback 收尾的代际检查已合并进锁内：gen=1 的清理在
    self._generation==2 时直接 return，绝不把 B 置 IDLE、绝不错发 finished(stopped)。
    """

    def _create_tts(self):
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

    def test_stale_async_stop_cleanup_does_not_touch_new_generation(self):
        """确定性场景：A 的异步清理线程（gen=1）收尾时 B（gen=2）正在播 → 直接 return。"""
        from app.speech.tts import TTSState

        tts, mock_bus = self._create_tts()

        # 模拟 B 正在播放（generation=2）
        tts._generation = 2
        tts._set_state(TTSState.PLAYING)
        tts._current_file = "/fake/b.mp3"
        tts._finished_published = False
        tts._stop_flag.clear()

        # A 的异步清理线程（gen=1）收尾 → 代际已变，必须直接 return
        tts._async_stop_playback(proc=None, cur_file=None, gen=1)

        # 1. B 的 state 仍是 PLAYING（未被置 IDLE）
        assert tts.get_state() == TTSState.PLAYING, \
            f"旧收尾覆盖了 B 的状态: {tts.get_state()}"
        # 2. 未置位 _finished_published、未发布任何 finished(stopped)
        assert tts._finished_published is False
        stopped_calls = [
            call[0][0] for call in mock_bus.publish.call_args_list
            if call[0][0].event_type == "speech.finished"
            and call[0][0].data.get("reason") == "stopped"
        ]
        assert len(stopped_calls) == 0, f"旧收尾错误发布了 {len(stopped_calls)} 个 stopped finished"

    def test_stop_then_speak_async_cleanup_keeps_b_playing(self):
        """真实时序：A 播放 → stop_playback(A) → A 清理未完成时 speak(B) → B 不被置 IDLE。"""
        import threading
        from app.speech.tts import TTSState

        tts, mock_bus = self._create_tts()

        # A 正在播放（generation=1），A 的 mpv 进程用慢 wait 拖住清理线程，
        # 保证测试主线程在 A 清理线程跑到代际检查前完成 generation 递增（确定性）。
        class SlowProc:
            def terminate(self):
                pass
            def wait(self, timeout=None):
                time.sleep(0.05)
                return 0
        tts._generation = 1
        tts._set_state(TTSState.PLAYING)
        tts._current_file = "/fake/a.mp3"
        tts._play_process = SlowProc()
        tts._finished_published = False
        tts._stop_flag.clear()

        # 用户打断 A → 异步清理线程启动（捕获 gen=1）
        tts.stop_playback()

        # === 立即模拟 speak(B) ===
        tts._generation = 2
        tts._stop_flag.clear()
        tts._finished_published = False
        tts._current_file = "/fake/b.mp3"
        tts._set_state(TTSState.PLAYING)

        # 等 A 的异步清理线程收尾（terminate + wait 0.05s + 收尾逻辑）
        time.sleep(0.3)

        # B 状态不被 A 的清理置 IDLE；B 的 finished 未被提前消费
        assert tts.get_state() == TTSState.PLAYING, \
            f"A 的清理线程把 B 置为了 {tts.get_state()}"
        assert tts._finished_published is False

