"""
PTT (Push-to-Talk) 麦克风录音

按住指定键开始录音，松开停止。
简单可靠，不做 VAD/唤醒词。
"""
import threading
import time
from typing import Callable, Optional

import pyaudio
import wave
import io


class PTTMicrophone:
    """
    PTT 麦克风录音器

    用法：
        mic = PTTMicrophone()
        mic.start()  # 开始监听按键
        # 按住空格键说话，松开自动停止并触发回调
        mic.stop()
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1,
                 chunk: int = 1024, ptt_key: str = "space",
                 on_recording_start: Optional[Callable] = None,
                 on_recording_stop: Optional[Callable[[bytes], None]] = None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk = chunk
        self.ptt_key = ptt_key
        self.on_recording_start = on_recording_start
        self.on_recording_stop = on_recording_stop

        self._pyaudio = None
        self._stream = None
        self._recording = False
        self._frames = []
        self._running = False
        self._listen_thread = None
        self._record_thread = None

    def start(self):
        """启动 PTT 监听"""
        if self._running:
            return

        self._pyaudio = pyaudio.PyAudio()
        self._running = True

        # 启动按键监听线程
        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="ptt-listen"
        )
        self._listen_thread.start()

        print(f"[PTT] 就绪，按住 {self.ptt_key} 开始说话")

    def stop(self):
        """停止 PTT"""
        self._running = False
        self._recording = False

        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        if self._pyaudio:
            self._pyaudio.terminate()
            self._pyaudio = None

    def _listen_loop(self):
        """按键监听循环（使用 keyboard 库全局监听）"""
        import keyboard

        ptt_key_map = {
            "space": "space",
            "ctrl": "ctrl",
            "shift": "shift",
            "alt": "alt",
        }
        kb_key = ptt_key_map.get(self.ptt_key, "space")

        while self._running:
            try:
                pressed = keyboard.is_pressed(kb_key)

                if pressed and not self._recording:
                    self._start_recording()
                elif not pressed and self._recording:
                    self._stop_recording()

                time.sleep(0.02)
            except Exception:
                time.sleep(0.1)

    def _start_recording(self):
        """开始录音"""
        self._recording = True
        self._frames = []

        try:
            self._stream = self._pyaudio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.chunk,
            )

            self._record_thread = threading.Thread(
                target=self._record_loop, daemon=True, name="ptt-record"
            )
            self._record_thread.start()

            if self.on_recording_start:
                try:
                    self.on_recording_start()
                except Exception:
                    pass

        except Exception as e:
            print(f"[PTT] 录音启动失败: {e}")
            self._recording = False

    def _record_loop(self):
        """录音循环"""
        while self._recording and self._stream:
            try:
                data = self._stream.read(self.chunk, exception_on_overflow=False)
                self._frames.append(data)
            except Exception:
                break

    def _stop_recording(self):
        """停止录音"""
        self._recording = False

        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        # 等待录音线程结束
        if self._record_thread:
            self._record_thread.join(timeout=1)
            self._record_thread = None

        # 构造音频数据
        audio_data = b''.join(self._frames)

        if self.on_recording_stop:
            try:
                self.on_recording_stop(audio_data)
            except Exception as e:
                print(f"[PTT] 回调异常: {e}")

        self._frames = []

    def is_recording(self) -> bool:
        return self._recording


class PTTMicrophoneManual(PTTMicrophone):
    """
    手动触发的 PTT 麦克风（用于测试）

    不监听按键，而是由外部代码调用 start_recording() / stop_recording()
    """

    def start(self):
        """初始化（不启动按键监听）"""
        if self._running:
            return
        self._pyaudio = pyaudio.PyAudio()
        self._running = True
        print("[PTT] 就绪（手动模式）")

    def start_recording(self):
        """手动开始录音"""
        if self._recording:
            return
        self._start_recording()

    def stop_recording(self):
        """手动停止录音"""
        if not self._recording:
            return
        self._stop_recording()
