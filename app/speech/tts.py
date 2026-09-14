"""
TTS 语音输出 - 可追踪、可停止、不阻塞

架构：
    Speech text
        ↓ TTSController.speak()
        ↓ 后台线程
        ↓ Edge TTS → temp .mp3
        ↓ 独立 mpv 进程播放
        ↓ 播放完成 → 清理 → 状态回到 IDLE

状态机：
    IDLE → GENERATING → PLAYING → IDLE
    IDLE → FAILED
    PLAYING → STOPPING → IDLE
"""
import asyncio
import os
import subprocess
import threading
import time
import uuid
from enum import Enum
from typing import Any, Dict, Optional

from ..core.event_bus import Event, EventType, get_event_bus


class TTSState(Enum):
    """TTS 状态"""
    IDLE = "idle"
    GENERATING = "generating"
    PLAYING = "playing"
    STOPPING = "stopping"
    FAILED = "failed"


class TTSError(Enum):
    """TTS 错误类型"""
    NONE = "none"
    GENERATION_FAILED = "generation_failed"
    NO_AUDIO = "no_audio"
    PLAYBACK_START_FAILED = "playback_start_failed"
    PLAYBACK_FAILED = "playback_failed"
    PLAYBACK_TIMEOUT = "playback_timeout"


class TTSController:
    """
    TTS 控制器

    职责：
    - Edge TTS 生成音频
    - 独立 mpv 进程播放
    - 可追踪播放状态
    - 可停止当前播放
    - 不阻塞 EventBus
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.engine = config.get("engine", "edge")
        self.voice = config.get("edge_voice", "zh-CN-XiaoxiaoNeural")
        self.rate = config.get("rate", "+0%")
        self.volume_pct = config.get("volume", "+0%")
        self.output_dir = config.get("output_dir", "./data/tts")
        self.enabled = config.get("enabled", True)
        self.mpv_path = config.get("mpv_path", r"D:\新建文件夹 (2)\mpv.exe")

        os.makedirs(self.output_dir, exist_ok=True)

        self.event_bus = get_event_bus()

        # 状态
        self._state = TTSState.IDLE
        self._lock = threading.Lock()

        # 播放进程
        self._play_process: Optional[subprocess.Popen] = None
        self._current_file: Optional[str] = None

        # asyncio 事件循环（用于 Edge TTS）
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

        # 播放完成监听线程
        self._watcher_thread: Optional[threading.Thread] = None

        # 停止标志
        self._stop_flag = threading.Event()

        self._running = False

    def start(self):
        """启动 TTS"""
        self._running = True

        # 启动 asyncio 事件循环
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="tts-loop"
        )
        self._loop_thread.start()

        # 订阅语音事件
        self.event_bus.subscribe(EventType.SPEECH_SAY, self._on_speech_say)

        print("[TTS] 已启动")

    def stop(self):
        """停止 TTS"""
        self._running = False
        self.stop_playback()

        if self._event_loop:
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)

    def _run_loop(self):
        """运行 asyncio 事件循环"""
        self._event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._event_loop)
        self._event_loop.run_forever()

    def _on_speech_say(self, event: Event):
        """处理语音播报事件（EventBus 回调）"""
        if not self.enabled:
            return

        text = event.get("text", "")
        priority = event.get("priority", "normal")

        if text:
            self.speak(text, priority)

    def speak(self, text: str, priority: str = "normal"):
        """
        播报文本（非阻塞）

        如果正在播放：
        - 高优先级：先停止当前，再播放新的
        - 普通优先级：停止当前，再播放新的（V1 简化，不做队列）
        """
        if not self.enabled or not text:
            return

        if not self._running:
            return

        # 如果正在播放，先停止
        if self.is_playing():
            self.stop_playback()

        # 提交到后台线程执行（不阻塞调用方）
        thread = threading.Thread(
            target=self._speak_thread,
            args=(text,),
            daemon=True,
            name="tts-speak",
        )
        thread.start()

    def _speak_thread(self, text: str):
        """后台线程：生成音频并播放"""
        self._stop_flag.clear()

        with self._lock:
            if self._state in (TTSState.GENERATING, TTSState.PLAYING):
                return
            self._set_state(TTSState.GENERATING)

        # Step 1: 生成音频
        output_file = self._generate_audio(text)

        # 检查是否被停止
        if self._stop_flag.is_set():
            self._cleanup_file(output_file)
            return

        if not output_file:
            self._set_state(TTSState.FAILED)
            self._publish_event("speech.failed", {"error": "generation_failed"})
            return

        self._current_file = output_file

        # Step 2: 播放
        success = self._play_audio(output_file)

        # 检查是否被停止
        if self._stop_flag.is_set():
            if self._play_process:
                try:
                    self._play_process.terminate()
                except:
                    pass
            self._cleanup_file(output_file)
            self._current_file = None
            return

        if not success:
            self._set_state(TTSState.FAILED)
            self._publish_event("speech.failed", {"error": "playback_failed"})
            self._cleanup_file(output_file)
            self._current_file = None
            return

        # Step 3: 等待播放完成
        self._wait_for_playback()

        # Step 4: 清理
        self._cleanup_file(output_file)
        self._current_file = None

        # 检查是否被停止（如果是 stop 触发的，状态已经被 stop_playback 设置为 IDLE）
        if not self._stop_flag.is_set():
            self._set_state(TTSState.IDLE)

    def _generate_audio(self, text: str) -> Optional[str]:
        """生成音频文件（同步，在后台线程执行）"""
        if self.engine != "edge":
            print(f"[TTS] 不支持的引擎: {self.engine}")
            return None

        try:
            import edge_tts

            # 生成唯一文件名
            filename = f"tts_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}.mp3"
            output_file = os.path.join(self.output_dir, filename)

            # 同步执行 asyncio 操作
            async def _gen():
                communicate = edge_tts.Communicate(
                    text=text,
                    voice=self.voice,
                    rate=self.rate,
                    volume=self.volume_pct,
                )
                await communicate.save(output_file)

            # 在我们的事件循环中运行
            if self._event_loop:
                future = asyncio.run_coroutine_threadsafe(_gen(), self._event_loop)
                future.result(timeout=10)  # 10 秒超时
            else:
                # 兜底：直接在当前线程运行
                asyncio.run(_gen())

            # 检查文件
            if os.path.exists(output_file) and os.path.getsize(output_file) > 1000:
                return output_file
            else:
                print("[TTS] 生成的音频文件无效")
                return None

        except Exception as e:
            print(f"[TTS] Edge TTS 生成失败: {e}")
            return None

    def _play_audio(self, filepath: str) -> bool:
        """启动 mpv 播放音频"""
        if not os.path.exists(self.mpv_path):
            print(f"[TTS] mpv 不存在: {self.mpv_path}")
            return False

        if not os.path.exists(filepath):
            print(f"[TTS] 音频文件不存在: {filepath}")
            return False

        try:
            args = [
                self.mpv_path,
                "--no-video",
                "--no-terminal",
                "--force-window=no",
                "--no-input-default-bindings",
                filepath,
            ]

            self._play_process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            self._set_state(TTSState.PLAYING)
            self._publish_event(EventType.SPEECH_STARTED, {"text": filepath})
            return True

        except Exception as e:
            print(f"[TTS] mpv 启动失败: {e}")
            return False

    def _wait_for_playback(self):
        """等待播放完成（阻塞在后台线程）"""
        if not self._play_process:
            return

        try:
            # 等待进程结束，最多 60 秒
            self._play_process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            print("[TTS] 播放超时，强制停止")
            self.stop_playback()

    def stop_playback(self):
        """停止当前播放"""
        with self._lock:
            if self._state not in (TTSState.PLAYING, TTSState.GENERATING):
                return

            self._set_state(TTSState.STOPPING)
            self._stop_flag.set()

        # 停止 mpv 进程
        if self._play_process:
            try:
                self._play_process.terminate()
                try:
                    self._play_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._play_process.kill()
            except Exception as e:
                print(f"[TTS] 停止播放失败: {e}")
            finally:
                self._play_process = None

        # 清理当前文件
        if self._current_file:
            self._cleanup_file(self._current_file)
            self._current_file = None

        with self._lock:
            self._set_state(TTSState.IDLE)

        self._publish_event(EventType.SPEECH_FINISHED, {"reason": "stopped"})

    def is_playing(self) -> bool:
        """是否正在播放"""
        with self._lock:
            return self._state in (TTSState.PLAYING, TTSState.GENERATING)

    def get_state(self) -> TTSState:
        """获取当前状态"""
        with self._lock:
            return self._state

    def set_enabled(self, enabled: bool):
        """设置是否启用"""
        self.enabled = enabled

    def _set_state(self, state: TTSState):
        """设置状态（内部调用，需要持有锁或单线程调用）"""
        self._state = state

    def _cleanup_file(self, filepath: str):
        """清理临时音频文件"""
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            print(f"[TTS] 清理临时文件失败: {e}")

    def _publish_event(self, event_type: str, data: Dict[str, Any]):
        """发布事件"""
        try:
            self.event_bus.publish(Event(
                event_type=event_type,
                data=data,
                source="tts",
            ))
        except Exception:
            pass

    def wait_finished(self, timeout: float = 30.0) -> bool:
        """等待播放完成（同步调用，用于测试）"""
        start = time.time()
        while time.time() - start < timeout:
            if not self.is_playing():
                return True
            time.sleep(0.1)
        return False
