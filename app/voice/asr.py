"""
语音识别 ASR 接口层

设计成可替换的接口：
- Qwen3ServerASR（主，llama-server HTTP API）
- Qwen3ASR（备用，llama.cpp 本地加载）

ASREngine 是统一接口，以后可以直接换模型。
"""
import abc
import threading
import time
from typing import Any


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

    @abc.abstractmethod
    def is_available(self) -> bool:
        """引擎是否可用"""

    @abc.abstractmethod
    def load(self):
        """加载模型"""

    @abc.abstractmethod
    def unload(self):
        """卸载模型，释放资源"""


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


class Qwen3ServerASR(ASREngine):
    """
    Qwen3-ASR llama-server HTTP API 引擎

    使用常驻 llama-server 服务，通过 OpenAI 兼容的 /audio/transcriptions 接口调用。
    不需要 llama-cpp-python，延迟更低（模型常驻）。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8080/v1",
                 model: str = "default", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = None
        self._loaded = False

    def load(self, retry_seconds: float = 0.0):
        """测试连接。retry_seconds>0 时：503/异常等 2 秒重试，总时长 ≤ retry_seconds。

        默认 0 = 单次探测（转写路径用，不阻塞调用线程）；
        首次加载（ASRManager.initialize / main 拉起）用 retry_seconds>0 等到模型真正就绪。
        日志降噪：只在状态变化（首次失败、恢复成功）时打印，避免每 2 秒刷屏完整异常。
        """
        deadline = time.monotonic() + retry_seconds
        announced_waiting = False
        while True:
            try:
                import requests
                resp = requests.get(f"{self.base_url}/models", timeout=0.5)
                if resp.status_code == 200:
                    self._loaded = True
                    if announced_waiting:
                        print(f"[ASR] llama-server 已恢复连接: {self.base_url}")
                    else:
                        print(f"[ASR] llama-server 已连接: {self.base_url}")
                    return
                # HTTP 非 200，计为一次失败
                if not announced_waiting:
                    print(f"[ASR] llama-server 连接中… HTTP {resp.status_code}（后续轮询静默）")
                    announced_waiting = True
            except Exception:
                if not announced_waiting:
                    print(f"[ASR] llama-server 连接中…（{self.base_url} 暂不可达，后续轮询静默）")
                    announced_waiting = True
            if retry_seconds <= 0 or time.monotonic() >= deadline:
                if announced_waiting and not self._loaded:
                    print("[ASR] llama-server 在等待窗口内未就绪，ASR 暂不可用"
                          "（不影响音乐/电影/LoL）")
                self._loaded = False
                return
            time.sleep(2)

    def transcribe(self, audio_data: bytes, sample_rate: int = 16000,
                   timeout: float | None = None) -> str:
        """转录音频。

        timeout：本次请求超时（秒）。None = 用实例默认 self.timeout（30s）。
        旁路打断识别可传更短超时（如 2s）独立控制，不影响主 ASR 请求。
        """
        if not self._loaded:
            return ""

        try:
            import io
            import wave

            import requests

            # 构造 WAV 文件
            buf = io.BytesIO()
            with wave.open(buf, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(audio_data)
            wav_bytes = buf.getvalue()

            files = {'file': ('audio.wav', wav_bytes, 'audio/wav')}
            data = {'model': self.model}

            resp = requests.post(
                f"{self.base_url}/audio/transcriptions",
                files=files,
                data=data,
                timeout=self.timeout if timeout is None else timeout,
            )

            if resp.status_code == 200:
                result = resp.json()
                text = result.get('text', '')
                # 提取 <asr_text> 内容
                import re
                match = re.search(r'<asr_text>(.*?)(?:</asr_text>|$)', text)
                if match:
                    text = match.group(1).strip()
                return text
            else:
                hint = ("（服务器未加载任何模型——请检查安装目录 models/ 下模型文件，"
                        "或查看启动日志 [ASR] 模型缺失/警告行）"
                        if resp.status_code == 400 else "")
                print(f"[ASR] 转录失败: HTTP {resp.status_code}{hint}")
                return ""

        except Exception as e:
            print(f"[ASR] 转录异常: {e}")
            return ""

    def is_available(self) -> bool:
        return self._loaded

    def unload(self):
        self._loaded = False


class ASRManager:
    """
    ASR 管理器 - 统一管理多个 ASR 引擎

    优先使用主引擎，失败时自动切换到备用引擎。
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.engines: list[ASREngine] = []
        self._active_engine: ASREngine | None = None
        self._lock = threading.Lock()

    def initialize(self, bootstrap_result: str = "unknown"):
        """初始化所有引擎。

        bootstrap_result:
          "ok"      — main.py 已确认 /v1/models data 非空；跳过 ensure_and_launch，
                      引擎单次探测（retry_seconds=0）即可。
          "fail"    — main.py 已确认 ASR 不可用；跳过 ensure_and_launch，
                      引擎单次探测，不做 30s 无意义等待。
          "unknown" — 旧调用路径：保持原有行为（再跑一次 ensure_and_launch + 30s 等待）。
        """
        engine_type = self.config.get("engine", "qwen3_server")

        # 主引擎：Qwen3-ASR llama-server（HTTP API）
        if engine_type == "qwen3_server":
            server_config = self.config.get("qwen3_server", {})
            # 轻量版自举（2026-10-18）：模型缺失→hf-mirror 国内镜像自动下载；
            # 8080 无服务→自动拉起 llama-server（退出时 atexit 连带清理，不留僵尸）。
            # 故障隔离：任何异常只打印，不阻断 ASR 使用外部服务。
            # 阶段 2：若 main.py 已经做过 bootstrap（ok/fail），跳过重复拉起。
            if bootstrap_result == "unknown":
                try:
                    from app.voice.asr_bootstrap import ensure_and_launch
                    ensure_and_launch(self.config)
                except Exception as e:
                    print(f"[ASR] 自举异常（ASR 仍可用外部 llama-server）: {e}")
            else:
                print(f"[ASR] 跳过重复 bootstrap（main.py 已确认结果={bootstrap_result}）")
            qwen_server = Qwen3ServerASR(
                base_url=server_config.get("base_url", "http://127.0.0.1:8080/v1"),
                model=server_config.get("model", "default"),
                timeout=server_config.get("timeout", 30.0),
            )
            self.engines.append(qwen_server)

        elif engine_type == "qwen3":
            # 备用：Qwen3-ASR 直接加载模型（需要 llama-cpp-python）
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

        # 加载第一个可用引擎。
        # bootstrap_result != "unknown" 时服务状态已由 main.py 确认，用单次探测（0s）；
        # 否则保留首次长等待（30s）。
        self._load_next_engine(initial=(bootstrap_result == "unknown"))

    def _load_next_engine(self, initial: bool = False) -> bool:
        """加载下一个可用引擎。

        initial=True（仅 initialize 首次加载）：允许长等待（最多 30 秒等模型就绪）；
        initial=False（转写/切换路径）：单次探测，不阻塞调用线程。
        """
        for engine in self.engines:
            if not engine.is_available():
                engine.load(retry_seconds=30.0 if initial else 0.0)
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
            # 无 active 引擎，或 active 引擎失联（服务中途挂掉/恢复）：
            # 重新走加载链——单次探测（retry_seconds=0，不阻塞调用线程），
            # 成功则继续转写；失败返回空串，下次调用再试（不永久失效）。
            if not self._active_engine or not self._active_engine.is_available():
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
        """卸载所有引擎并清空列表（stop() 后下次 start() → initialize() 不重复注册）。"""
        for engine in self.engines:
            try:
                engine.unload()
            except Exception as e:
                print(f"[ASR] 引擎卸载异常（继续清理其他）: {e}")
        self.engines.clear()
        self._active_engine = None
