"""用户主动触发的短音频采集原型。

本模块只负责一次性采集麦克风并写出临时 WAV，不连接识曲服务、不监听后台，
也不接入现有 ASR/VAD 或正式识曲入口。后续系统声音回环应实现独立的 provider。
"""
from __future__ import annotations

import os
import wave


class CaptureError(Exception):
    """采集失败，调用方应向用户展示明确原因。"""


class CaptureCancelled(CaptureError):
    """用户主动取消采集。"""


class MicrophoneCapture:
    """使用项目已有 PyAudio 依赖完成一次有限时长的麦克风采集。"""

    def __init__(self, seconds: int = 8, sample_rate: int = 16_000,
                 channels: int = 1, chunk: int = 1024):
        if seconds <= 0 or sample_rate <= 0 or channels <= 0 or chunk <= 0:
            raise ValueError("采集参数必须为正数")
        self.seconds = int(seconds)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.chunk = int(chunk)

    def capture(self, out_path: str, cancel_event=None) -> dict[str, object]:
        """采集一次并写 WAV；cancel_event 被设置时立即停止并抛出取消异常。"""
        try:
            import pyaudio
        except ImportError as exc:
            raise CaptureError(f"PyAudio 不可用: {exc}") from exc

        pa = pyaudio.PyAudio()
        stream = None
        frames: list[bytes] = []
        remaining = self.seconds * self.sample_rate
        try:
            try:
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    frames_per_buffer=self.chunk,
                )
            except Exception as exc:
                raise CaptureError(f"麦克风不可用或被占用: {exc}") from exc

            while remaining > 0:
                if cancel_event is not None and cancel_event.is_set():
                    raise CaptureCancelled("用户取消了采集")
                count = min(self.chunk, remaining)
                try:
                    frames.append(stream.read(count, exception_on_overflow=False))
                except Exception as exc:
                    raise CaptureError(f"读取麦克风数据失败: {exc}") from exc
                remaining -= count
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                finally:
                    stream.close()
            pa.terminate()

        if cancel_event is not None and cancel_event.is_set():
            raise CaptureCancelled("用户取消了采集")

        try:
            parent = os.path.dirname(os.path.abspath(out_path))
            os.makedirs(parent, exist_ok=True)
            with wave.open(out_path, "wb") as wav:
                wav.setnchannels(self.channels)
                wav.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
                wav.setframerate(self.sample_rate)
                wav.writeframes(b"".join(frames))
        except Exception as exc:
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise CaptureError(f"保存临时音频失败: {exc}") from exc

        return {"path": out_path, "seconds": self.seconds,
                "sample_rate": self.sample_rate, "channels": self.channels}
