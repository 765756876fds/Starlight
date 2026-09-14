"""
语音识别 ASR 接口层

设计成可替换的接口：
- Qwen3ASR（主候选，本地运行）
- FasterWhisperASR（备用）

ASREngine 是统一接口，以后可以直接换模型。
"""
import abc
import threading
import time
from typing import Any, Dict, List, Optional


class ASREngine(abc.ABC):
    """ASR 引擎基类"""

    @abc.abstractmethod
    def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """
        转录音频为文字

        Args:
            audio_data: 音频二进制数据（PCM 16-bit）
            sample_rate: 采样率

        Returns:
            识别出的文字
        """
        pass

    @abc.abstractmethod
    def is_available(self) -> bool:
        """引擎是否可用"""
        pass

    @abc.abstractmethod
    def load(self):
        """加载模型"""
        pass

    @abc.abstractmethod
    def unload(self):
        """卸载模型，释放资源"""
        pass


class Qwen3ASR(ASREngine):
    """
    Qwen3-ASR 本地语音识别

    使用 llama-cpp-python 加载 GGUF 模型。
    需要 mmproj 多模态投影文件。
    """

    def __init__(self, model_path: str, mmproj_path: str,
                 n_threads: int = 4, n_ctx: int = 2048):
        self.model_path = model_path
        self.mmproj_path = mmproj_path
        self.n_threads = n_threads
        self.n_ctx = n_ctx
        self._model = None
        self._lock = threading.Lock()
        self._loaded = False

    def load(self):
        """加载模型"""
        if self._loaded:
            return

        try:
            from llama_cpp import Llama
            from llama_cpp.llama_chat_format import Llava15ChatHandler

            # 加载多模态投影
            chat_handler = Llava15ChatHandler(clip_model_path=self.mmproj_path)

            # 加载模型
            self._model = Llama(
                model_path=self.model_path,
                chat_handler=chat_handler,
                n_ctx=self.n_ctx,
                n_threads=self.n_threads,
                verbose=False,
            )
            self._loaded = True
            print(f"[ASR] Qwen3-ASR 模型已加载: {self.model_path}")
        except ImportError:
            print("[ASR] llama-cpp-python 未安装，Qwen3-ASR 不可用")
        except Exception as e:
            print(f"[ASR] Qwen3-ASR 加载失败: {e}")

    def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """转录音频"""
        if not self._loaded or not self._model:
            return ""

        with self._lock:
            try:
                # Qwen3-ASR 使用多模态方式输入音频
                # 将音频数据编码为 base64 data URL
                import base64
                audio_b64 = base64.b64encode(audio_data).decode()
                audio_url = f"data:audio/wav;base64,{audio_b64}"

                messages = [
                    {
                        "role": "system",
                        "content": "You are a speech recognition model. Transcribe the audio to text. Output only the transcribed text, no explanation."
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "audio_url", "audio_url": {"url": audio_url}},
                            {"type": "text", "text": "Transcribe this audio to Chinese text."}
                        ]
                    }
                ]

                response = self._model.create_chat_completion(
                    messages=messages,
                    temperature=0.0,
                    max_tokens=512,
                )

                text = response["choices"][0]["message"]["content"].strip()
                return text
            except Exception as e:
                print(f"[ASR] 转录失败: {e}")
                return ""

    def is_available(self) -> bool:
        return self._loaded and self._model is not None

    def unload(self):
        """卸载模型"""
        self._model = None
        self._loaded = False
        import gc
        gc.collect()


class FasterWhisperASR(ASREngine):
    """
    Faster-Whisper ASR（备用引擎）

    配合 Silero VAD 使用。
    """

    def __init__(self, model_size: str = "small",
                 device: str = "cpu", compute_type: str = "int8"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._loaded = False

    def load(self):
        """加载模型"""
        if self._loaded:
            return
        try:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
            self._loaded = True
            print(f"[ASR] Faster-Whisper 模型已加载: {self.model_size}")
        except ImportError:
            print("[ASR] faster-whisper 未安装")
        except Exception as e:
            print(f"[ASR] Faster-Whisper 加载失败: {e}")

    def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """转录音频"""
        if not self._loaded or not self._model:
            return ""

        try:
            import numpy as np
            import io
            import wave

            # 将 PCM 数据转为 numpy 数组
            audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            segments, _ = self._model.transcribe(
                audio_array,
                language="zh",
                beam_size=5,
                vad_filter=True,
            )

            text = " ".join(seg.text.strip() for seg in segments)
            return text
        except Exception as e:
            print(f"[ASR] Faster-Whisper 转录失败: {e}")
            return ""

    def is_available(self) -> bool:
        return self._loaded and self._model is not None

    def unload(self):
        self._model = None
        self._loaded = False


class ASRManager:
    """
    ASR 管理器 - 统一管理多个 ASR 引擎

    优先使用主引擎，失败时自动切换到备用引擎。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.engines: List[ASREngine] = []
        self._active_engine: Optional[ASREngine] = None
        self._lock = threading.Lock()

    def initialize(self):
        """初始化所有引擎"""
        engine_type = self.config.get("engine", "faster_whisper")

        if engine_type == "qwen3":
            # 主引擎：Qwen3-ASR（需要 llama-cpp-python）
            try:
                import llama_cpp
                qwen = Qwen3ASR(
                    model_path=self.config.get("qwen3_asr_model", ""),
                    mmproj_path=self.config.get("qwen3_asr_mmproj", ""),
                    n_threads=self.config.get("asr_max_threads", 4),
                )
                self.engines.append(qwen)
            except ImportError:
                print("[ASR] llama-cpp-python 未安装，跳过 Qwen3-ASR")

        # Faster-Whisper 引擎（默认）
        fw_config = self.config.get("faster_whisper", {})
        faster = FasterWhisperASR(
            model_size=fw_config.get("model_size", "small"),
            device=fw_config.get("device", "cpu"),
            compute_type=fw_config.get("compute_type", "int8"),
        )
        self.engines.append(faster)

        # 加载第一个可用引擎
        self._load_next_engine()

    def _load_next_engine(self) -> bool:
        """加载下一个可用引擎"""
        for engine in self.engines:
            if not engine.is_available():
                engine.load()
                if engine.is_available():
                    self._active_engine = engine
                    return True
        return False

    def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """
        转录音频

        优先使用当前引擎，失败时自动切换。
        """
        with self._lock:
            if not self._active_engine:
                if not self._load_next_engine():
                    return ""

            try:
                text = self._active_engine.transcribe(audio_data, sample_rate)
                if text:
                    return text
            except Exception as e:
                print(f"[ASR] 引擎转录失败，尝试切换: {e}")

            # 尝试切换引擎
            for engine in self.engines:
                if engine != self._active_engine:
                    if not engine.is_available():
                        engine.load()
                    if engine.is_available():
                        self._active_engine = engine
                        try:
                            return engine.transcribe(audio_data, sample_rate)
                        except Exception:
                            continue

            return ""

    def is_available(self) -> bool:
        return self._active_engine is not None and self._active_engine.is_available()

    def unload_all(self):
        """卸载所有引擎"""
        for engine in self.engines:
            engine.unload()
        self._active_engine = None
