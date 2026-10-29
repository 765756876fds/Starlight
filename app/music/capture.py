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


class SystemLoopbackCapture:
    """通过 Windows WASAPI loopback 一次性采集系统播放混音。

    依赖 SoundCard 的 WASAPI backend；它与 PyAudio 麦克风输入是两条独立链路。
    模块按块采集以支持取消，不会在应用启动时建立或持续保留音频流。
    """

    def __init__(self, seconds: int = 8, sample_rate: int = 48_000,
                 channels: int = 2, chunk_seconds: float = 0.25,
                 recorder_factory=None):
        if seconds <= 0 or sample_rate <= 0 or channels <= 0 or chunk_seconds <= 0:
            raise ValueError("采集参数必须为正数")
        self.seconds = int(seconds)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.chunk_seconds = float(chunk_seconds)
        self._recorder_factory = recorder_factory

    def _recorder(self):
        if self._recorder_factory is not None:
            return self._recorder_factory()
        try:
            import soundcard as sc
        except ImportError as exc:
            raise CaptureError(
                "系统声音采集需要 SoundCard（Windows WASAPI）依赖；未安装或不可用"
            ) from exc
        try:
            speaker = sc.default_speaker()
            if speaker is None:
                raise CaptureError("没有可用的 Windows 默认播放设备")
            # SoundCard 0.4.x 的回环设备通过 get_microphone(...,
            # include_loopback=True) 获取；_Speaker 没有 loopback() 方法。
            return sc.get_microphone(speaker.id, include_loopback=True)
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(f"无法打开 Windows WASAPI 回环设备: {exc}") from exc

    def capture(self, out_path: str, cancel_event=None) -> dict[str, object]:
        """采集系统混音并写入 16-bit PCM WAV；只在本次调用期间打开回环。"""
        try:
            import numpy as np
        except ImportError as exc:
            raise CaptureError(f"系统声音采集需要 numpy: {exc}") from exc

        recorder = self._recorder()
        blocks = []
        remaining = self.seconds * self.sample_rate
        block_size = max(1, int(self.chunk_seconds * self.sample_rate))
        try:
            while remaining > 0:
                if cancel_event is not None and cancel_event.is_set():
                    raise CaptureCancelled("用户取消了系统声音采集")
                count = min(block_size, remaining)
                try:
                    block = recorder.record(
                        samplerate=self.sample_rate,
                        numframes=count,
                        channels=self.channels,
                    )
                except Exception as exc:
                    raise CaptureError(f"读取 Windows 系统声音失败: {exc}") from exc
                array = np.asarray(block)
                if array.ndim == 1:
                    array = array[:, None]
                if array.size == 0 or array.shape[1] != self.channels:
                    raise CaptureError("Windows 回环返回了空音频或通道数不匹配")
                blocks.append(array)
                remaining -= len(array)
                if len(array) == 0:
                    raise CaptureError("Windows 回环没有返回音频帧")
        finally:
            close = getattr(recorder, "close", None)
            if close is not None:
                close()

        if cancel_event is not None and cancel_event.is_set():
            raise CaptureCancelled("用户取消了系统声音采集")

        try:
            samples = np.concatenate(blocks, axis=0)
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
            parent = os.path.dirname(os.path.abspath(out_path))
            os.makedirs(parent, exist_ok=True)
            with wave.open(out_path, "wb") as wav:
                wav.setnchannels(self.channels)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
                wav.writeframes(pcm)
        except Exception as exc:
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise CaptureError(f"保存系统声音临时音频失败: {exc}") from exc

        return {"path": out_path, "seconds": self.seconds,
                "sample_rate": self.sample_rate, "channels": self.channels}
