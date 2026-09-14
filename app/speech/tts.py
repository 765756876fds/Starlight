"""
TTS 语音输出

第一阶段：Edge TTS（免费、质量好、无需本地模型）
以后可切换到 Kokoro（本地）。
"""
import asyncio
import os
import threading
import time
from typing import Any, Dict, Optional

from ..core.event_bus import EventBus, Event, EventType, get_event_bus


class TTSController:
    """
    TTS 控制器

    支持 Edge TTS，异步生成和播放。
    通过 EventBus 接收 SPEECH_SAY 事件。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.engine = config.get("engine", "edge")
        self.voice = config.get("edge_voice", "zh-CN-XiaoxiaoNeural")
        self.rate = config.get("rate", "+0%")
        self.volume = config.get("volume", "+0%")
        self.output_dir = config.get("output_dir", "./data/tts")
        self.enabled = config.get("enabled", True)

        os.makedirs(self.output_dir, exist_ok=True)

        self.event_bus = get_event_bus()
        self._is_speaking = False
        self._lock = threading.Lock()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

    def start(self):
        """启动 TTS"""
        # 在独立线程中运行 asyncio 事件循环
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="tts-loop"
        )
        self._loop_thread.start()

        # 订阅语音事件
        self.event_bus.subscribe(EventType.SPEECH_SAY, self._on_speech_say)

        print("[TTS] 已启动")

    def stop(self):
        """停止 TTS"""
        if self._event_loop:
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)

    def _run_loop(self):
        """运行 asyncio 事件循环"""
        self._event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._event_loop)
        self._event_loop.run_forever()

    def _on_speech_say(self, event: Event):
        """处理语音播报事件"""
        if not self.enabled:
            return

        text = event.get("text", "")
        priority = event.get("priority", "normal")

        if text:
            self.say(text, priority)

    def say(self, text: str, priority: str = "normal"):
        """
        播报文本

        Args:
            text: 要播报的文本
            priority: 优先级 high / normal / low
        """
        if not self.enabled or not text:
            return

        # 高优先级可以打断当前播报
        if priority == "high" and self._is_speaking:
            self._stop_current_playback()

        # 提交到 asyncio 循环
        if self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._speak_async(text), self._event_loop
            )

    async def _speak_async(self, text: str):
        """异步生成并播放语音"""
        with self._lock:
            if self._is_speaking:
                return
            self._is_speaking = True

        try:
            # 生成音频文件
            timestamp = int(time.time() * 1000)
            output_file = os.path.join(self.output_dir, f"tts_{timestamp}.mp3")

            if self.engine == "edge":
                await self._edge_tts(text, output_file)
            else:
                print(f"[TTS] 不支持的引擎: {self.engine}")
                return

            # 播放音频
            if os.path.exists(output_file):
                self._play_audio(output_file)

                # 发布事件
                self.event_bus.publish(Event(
                    event_type=EventType.SPEECH_STARTED,
                    data={"text": text, "file": output_file},
                    source="tts"
                ))

        except Exception as e:
            print(f"[TTS] 播报失败: {e}")
        finally:
            self._is_speaking = False

    async def _edge_tts(self, text: str, output_file: str):
        """使用 Edge TTS 生成语音"""
        try:
            import edge_tts
            communicate = edge_tts.Communicate(
                text=text,
                voice=self.voice,
                rate=self.rate,
                volume=self.volume,
            )
            await communicate.save(output_file)
        except ImportError:
            print("[TTS] edge-tts 未安装")
        except Exception as e:
            print(f"[TTS] Edge TTS 生成失败: {e}")

    def _play_audio(self, filepath: str):
        """播放音频文件（使用系统默认播放器，无窗口）"""
        try:
            # 使用 mpv 播放 TTS（如果可用），否则用系统默认
            # 这里用简单的方式：通过 winsound 或 mpv
            if os.name == "nt":
                # Windows: 使用 mpv 无窗口播放
                mpv_path = self.config.get("mpv_path", "")
                if mpv_path and os.path.exists(mpv_path):
                    import subprocess
                    subprocess.Popen(
                        [mpv_path, "--no-video", "--no-terminal",
                         "--force-window=no", filepath],
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    # 兜底：使用 winsound（只支持 wav）
                    import winsound
                    winsound.PlaySound(filepath, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            print(f"[TTS] 音频播放失败: {e}")

    def _stop_current_playback(self):
        """停止当前播放（简化实现）"""
        # 实际实现需要跟踪播放进程
        pass

    def is_speaking(self) -> bool:
        """是否正在播报"""
        return self._is_speaking

    def set_enabled(self, enabled: bool):
        """设置是否启用"""
        self.enabled = enabled
