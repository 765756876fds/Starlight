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

from app.core.config import resolve_mpv_path
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
        # mpv 查找链（打包零配置运行）：settings 显式 → 打包内置 bundled → PATH（下游兜底）
        self.mpv_path = resolve_mpv_path(config.get("mpv_path"))

        # Qwen（阿里云百炼）引擎配置
        self.qwen_api_key = self._resolve_qwen_key(config)
        self.qwen_base_url = config.get("qwen_base_url", "https://dashscope.aliyuncs.com/api/v1")
        self.qwen_model = config.get("qwen_model", "qwen-audio-3.1-tts-flash")
        self.qwen_voice = config.get("qwen_voice", "longanlingxin_v3.1")
        try:
            self.qwen_rate = float(config.get("qwen_rate", 1.0))
            self.qwen_volume = int(config.get("qwen_volume", 50))
        except Exception:
            self.qwen_rate = 1.0
            self.qwen_volume = 50

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

        # 防止重复发布 speech.finished
        self._finished_published = False

        # generation 计数器，防止旧线程继续执行
        self._generation = 0

        self._running = False

    def set_voice(self, voice: str):
        if self.engine == "qwen":
            self.qwen_voice = voice
        else:
            self.voice = voice

    def set_rate(self, rate):
        if self.engine == "qwen":
            try:
                self.qwen_rate = float(rate)
            except Exception:
                self.qwen_rate = 1.0
        else:
            self.rate = rate

    def set_volume(self, volume):
        if self.engine == "qwen":
            try:
                self.qwen_volume = int(volume)
            except Exception:
                self.qwen_volume = 50
        else:
            self.volume_pct = volume

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

        # 递增 generation，让旧线程失效
        self._generation += 1
        my_generation = self._generation

        # 清除停止标志，允许新播放
        self._stop_flag.clear()
        # 重置 finished 标记，允许新播放发布 finished
        self._finished_published = False

        # 提交到后台线程执行（不阻塞调用方）
        thread = threading.Thread(
            target=self._speak_thread,
            args=(text, my_generation),
            daemon=True,
            name="tts-speak",
        )
        thread.start()

    def _speak_thread(self, text: str, generation: int):
        """后台线程：生成音频并播放"""
        # 检查是否已经被新的 speak 取代
        if generation != self._generation:
            return

        with self._lock:
            if self._state in (TTSState.GENERATING, TTSState.PLAYING):
                return
            self._set_state(TTSState.GENERATING)

        # Step 1: 生成音频
        output_file = self._generate_audio(text)

        # 检查是否已经被新的 speak 取代
        if generation != self._generation:
            self._cleanup_file(output_file)
            return

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

        # 检查是否已经被新的 speak 取代
        if generation != self._generation:
            if self._play_process:
                try:
                    self._play_process.terminate()
                except Exception:
                    pass
            self._cleanup_file(output_file)
            self._current_file = None
            return

        # 检查是否被停止
        if self._stop_flag.is_set():
            if self._play_process:
                try:
                    self._play_process.terminate()
                except Exception:
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

        # 旧线程防护：如果在等待期间新的 speak() 启动了（generation 已变），
        # 本线程不得再清理共享状态或发布事件，否则会覆盖新线程的 state、
        # 清空新线程的 _current_file、错误发布 SPEECH_FINISHED(completed)。
        if generation != self._generation:
            return

        # Step 4: 清理
        self._cleanup_file(output_file)
        self._current_file = None

        # 检查是否被停止（如果是 stop 触发的，状态已经被 stop_playback 设置为 IDLE）
        if not self._stop_flag.is_set():
            # _finished_published 加锁保护（与 tts-stop 异步收尾线程互斥）
            publish_done = False
            with self._lock:
                self._set_state(TTSState.IDLE)
                if not self._finished_published:
                    self._finished_published = True
                    publish_done = True
            if publish_done:
                self._publish_event(EventType.SPEECH_FINISHED, {"reason": "completed"})

    def has_qwen_key(self) -> bool:
        """Qwen API Key 是否已配置（环境变量 / settings 值 / tools/.qwen_key）。"""
        return bool(self._resolve_qwen_key(self.config))

    def _resolve_qwen_key(self, config: Dict[str, Any]) -> str:
        """Qwen API Key：环境变量 QWEN_API_KEY > settings 值 > tools/.qwen_key（已 gitignore）"""
        env_key = os.environ.get("QWEN_API_KEY", "")
        if env_key:
            return env_key
        cfg_key = config.get("qwen_api_key", "")
        if cfg_key:
            return cfg_key
        key_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "tools", ".qwen_key",
        )
        if os.path.exists(key_file):
            try:
                with open(key_file, encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
        return ""

    def _generate_audio(self, text: str) -> Optional[str]:
        """生成音频文件（同步，在后台线程执行）"""
        if self.engine == "qwen":
            output_file = self._generate_qwen_audio(text)
            if output_file:
                return output_file
            print("[TTS] Qwen 生成失败，回退 Edge TTS")
        elif self.engine != "edge":
            print(f"[TTS] 不支持的引擎: {self.engine}")
            return None

        try:
            import edge_tts

            # 生成唯一文件名（重试复用同一路径覆盖写，避免失败一次残留一个文件）
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

            # Edge TTS 403（微软端点限流/风控）：等 1s→3s 重试 2 次，最多多等 4 秒；
            # 非 403 异常直接放弃（保持现有降级），不阻塞太久
            for attempt, wait in ((1, 0), (2, 1), (3, 3)):
                try:
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
                    if ("403" in str(e) or "Invalid response status" in str(e)) and attempt < 3:
                        print(f"[TTS] Edge TTS 生成失败（HTTP 403，第 {attempt} 次），等待 {wait} 秒重试")
                        time.sleep(wait)
                        continue
                    print(f"[TTS] Edge TTS 生成失败: {e}")
                    # 3 次全失败：清理残留文件（避免失败一次留一个垃圾）
                    try:
                        if os.path.exists(output_file):
                            os.remove(output_file)
                    except Exception:
                        pass
                    return None
            print("[TTS] Edge TTS 生成失败（3 次重试后放弃）")
            return None

        except Exception as e:
            print(f"[TTS] Edge TTS 生成失败: {e}")
            return None

    def _generate_qwen_audio(self, text: str) -> Optional[str]:
        """阿里云百炼 Qwen TTS 合成（同步，在后台线程执行）"""
        if not self.qwen_api_key:
            print("[TTS] Qwen API Key 未配置（settings.yaml / tools/.qwen_key / QWEN_API_KEY）")
            return None

        filename = f"tts_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}.wav"
        output_file = os.path.join(self.output_dir, filename)

        try:
            import requests

            url = self.qwen_base_url.rstrip("/") + "/services/audio/tts/SpeechSynthesizer"
            body = {
                "model": self.qwen_model,
                "input": {
                    "text": text,
                    "voice": self.qwen_voice,
                    "format": "wav",
                    "sample_rate": 24000,
                    "rate": self.qwen_rate,      # 语速 0.5~2.0（float）
                    "volume": self.qwen_volume,  # 音量 0~100（int）
                },
            }
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.qwen_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30,
            )
            if resp.status_code != 200:
                print(f"[TTS] Qwen 合成失败: HTTP {resp.status_code} {resp.text[:200]}")
                return None
            data = resp.json()
            audio = data.get("output", {}).get("audio", {})
            if not audio.get("url"):
                print(f"[TTS] Qwen 响应无音频 URL: {data.get('code', resp.text[:200])}")
                return None

            # 下载音频：OSS 国内域名偶尔 DNS 抖动/瞬时失败，重试 2 次（间隔 1s）；
            # 3 次全失败返回 None → 外层回退 Edge TTS（保持）
            audio_bytes = None
            for attempt in range(1, 4):
                try:
                    audio_resp = requests.get(audio["url"], timeout=30)
                    audio_resp.raise_for_status()
                    audio_bytes = audio_resp.content
                    break
                except Exception as e:
                    print(f"[TTS] Qwen 音频下载失败（第 {attempt} 次）: {e}")
                    if attempt < 3:
                        time.sleep(1)
            if audio_bytes is None:
                return None
            with open(output_file, "wb") as f:
                f.write(audio_bytes)

            if os.path.exists(output_file) and os.path.getsize(output_file) > 1000:
                return output_file
            print("[TTS] Qwen 生成的音频文件无效")
            return None
        except Exception as e:
            print(f"[TTS] Qwen TTS 生成失败: {e}")
            try:
                if os.path.exists(output_file):
                    os.remove(output_file)
            except Exception:
                pass
            return None

    def _resolve_mpv_path(self) -> bool:
        """解析 mpv：配置(isfile) → PATH(which)。
        照抄 music 链 mpv.py:_resolve_mpv_path 范本。
        显式配置的路径不自动替换（尊重配置语义）。"""
        if os.path.isfile(self.mpv_path):
            return True
        import shutil
        found = shutil.which(self.mpv_path)
        if found:
            self.mpv_path = found
            return True
        print(f"[TTS] mpv 不存在: {self.mpv_path}")
        return False


    def _play_audio(self, filepath: str) -> bool:
        """启动 mpv 播放音频"""
        if not self._resolve_mpv_path():
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
        """停止当前播放（非阻塞，瞬间返回）。

        锁内只置 STOPPING + 发 _stop_flag 信号；真正的进程终止、
        文件清理与收尾置 IDLE 放到独立 daemon 线程（tts-stop）异步执行。
        → 无论谁调用（UI 线程 / 识别线程 / SpeechQueue worker）都不会被
        mpv terminate/wait 阻塞。
        """
        with self._lock:
            if self._state not in (TTSState.PLAYING, TTSState.GENERATING):
                return

            self._set_state(TTSState.STOPPING)
            self._stop_flag.set()

        # 捕获当前进程/文件引用 + 代际：异步线程只清理自己的，
        # 新 speak 若已复用这些引用则不动（防覆盖新状态）。
        proc = self._play_process
        cur_file = self._current_file
        gen = self._generation
        threading.Thread(
            target=self._async_stop_playback,
            args=(proc, cur_file, gen),
            daemon=True,
            name="tts-stop",
        ).start()

    def _async_stop_playback(self, proc, cur_file, gen):
        """异步执行真正的终止 / 清理 / 收尾（不阻塞任何调用方）。"""
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
            except Exception as e:
                print(f"[TTS] 停止播放失败: {e}")

        if cur_file:
            try:
                self._cleanup_file(cur_file)
            except Exception:
                pass

        with self._lock:
            # 只清理本线程捕获的引用（新 speak 已复用的不碰）
            if self._play_process is proc:
                self._play_process = None
            if self._current_file is cur_file:
                self._current_file = None

        # 代际未变（没有被新 speak 取代）才收尾置 IDLE + 发布 finished。
        # 代际检查与状态修改合并进锁内（防检查后、进锁前新 speak 启动 →
        # 旧线程误置 IDLE 覆盖新状态）；_finished_published 同锁保护。
        publish_stopped = False
        with self._lock:
            if self._generation != gen:
                return
            self._set_state(TTSState.IDLE)
            if not self._finished_published:
                self._finished_published = True
                publish_stopped = True
        if publish_stopped:
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
