"""
语音运行时 - 整合 ASR + Intent + 命令执行

负责：
- 麦克风音频采集（PTT 或持续监听）
- VAD 语音活动检测
- ASR 语音识别
- Intent 意图解析
- 命令执行（通过 CommandBus）
"""
import threading
import time
from typing import Any, Dict, Optional

from ..core.event_bus import EventBus, Event, EventType, get_event_bus
from ..core.command_bus import CommandBus, Command, CommandType, get_command_bus
from ..core.state_store import StateStore, StateNamespace, get_state_store

from .asr import ASRManager
from .intent import IntentRouter, Intent


class VoiceRuntime:
    """
    语音运行时

    整合语音输入全流程。
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.asr_config = config.get("asr", {})
        self.intent_config = config.get("intent", {})

        # 子系统
        self.asr = ASRManager(self.asr_config)
        self.intent_router = IntentRouter(self.intent_config)

        # 核心服务
        self.event_bus = get_event_bus()
        self.command_bus = get_command_bus()
        self.state_store = get_state_store()

        # 状态
        self._running = False
        self._listening = False
        self._asr_enabled = True

        # 麦克风
        self._audio_stream = None
        self._mic_thread: Optional[threading.Thread] = None

    def start(self):
        """启动语音运行时"""
        if self._running:
            return

        self._running = True

        # 初始化 ASR（加载模型可能需要时间）
        print("[Voice] 正在加载 ASR 模型...")
        self.asr.initialize()

        # 初始化 Intent
        print("[Voice] 正在加载 Intent 模型...")
        self.intent_router.initialize()

        # 启动麦克风监听
        self._start_microphone()

        self.state_store.set(StateNamespace.VOICE, "enabled", True)
        print("[Voice] 运行时已启动")

    def stop(self):
        """停止语音运行时"""
        self._running = False
        self._listening = False
        self.asr.unload_all()
        self.intent_router.unload()
        print("[Voice] 运行时已停止")

    def _start_microphone(self):
        """启动麦克风采集线程"""
        self._mic_thread = threading.Thread(
            target=self._mic_loop, daemon=True, name="voice-mic"
        )
        self._mic_thread.start()

    def _mic_loop(self):
        """麦克风采集循环"""
        try:
            import pyaudio
        except ImportError:
            print("[Voice] pyaudio 未安装，麦克风不可用")
            return

        audio = pyaudio.PyAudio()
        sample_rate = int(self.asr_config.get("sample_rate", 16000))
        chunk_size = 1024

        try:
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=sample_rate,
                input=True,
                frames_per_buffer=chunk_size,
            )
        except Exception as e:
            print(f"[Voice] 麦克风打开失败: {e}")
            return

        self._listening = True
        print("[Voice] 麦克风已启动，正在监听...")

        # 简单的 VAD：基于音量阈值
        silence_threshold = 500  # 音量阈值
        silence_duration = 0
        max_silence = 30  # 静音帧数（约 0.7 秒）
        min_speech = 5    # 最小语音帧数
        speech_frames = []
        in_speech = False
        speech_count = 0

        while self._running and self._listening:
            try:
                data = stream.read(chunk_size, exception_on_overflow=False)
                volume = self._calculate_volume(data)

                if volume > silence_threshold:
                    # 检测到语音
                    silence_duration = 0
                    if not in_speech:
                        in_speech = True
                        speech_count = 0
                    speech_frames.append(data)
                    speech_count += 1
                else:
                    # 静音
                    if in_speech:
                        silence_duration += 1
                        speech_frames.append(data)

                        if silence_duration >= max_silence:
                            # 语音结束
                            if speech_count >= min_speech:
                                self._process_speech(b"".join(speech_frames), sample_rate)
                            in_speech = False
                            speech_frames = []
                            speech_count = 0
                            silence_duration = 0

            except Exception as e:
                print(f"[Voice] 麦克风读取异常: {e}")
                time.sleep(0.1)

        stream.stop_stream()
        stream.close()
        audio.terminate()
        self._listening = False

    def _calculate_volume(self, data: bytes) -> int:
        """计算音频音量（RMS）"""
        import struct
        if len(data) < 2:
            return 0
        samples = struct.unpack(f"<{len(data)//2}h", data)
        if not samples:
            return 0
        return int(sum(abs(s) for s in samples) / len(samples))

    def _process_speech(self, audio_data: bytes, sample_rate: int):
        """处理一段语音"""
        if not self._asr_enabled:
            return

        # ASR 识别
        text = self.asr.transcribe(audio_data, sample_rate)
        if not text or not text.strip():
            return

        print(f"[Voice] 识别: {text}")

        # 发布识别结果事件
        self.event_bus.publish(Event(
            event_type=EventType.VOICE_TEXT_RECEIVED,
            data={"text": text},
            source="voice"
        ))

        # 意图解析
        intent = self.intent_router.parse(text)
        print(f"[Voice] 意图: {intent}")

        # 发布意图事件
        self.event_bus.publish(Event(
            event_type=EventType.VOICE_INTENT_PARSED,
            data={"intent": intent.to_dict(), "original_text": text},
            source="voice"
        ))

        # 执行命令
        self._execute_intent(intent, text)

    def _execute_intent(self, intent: Intent, original_text: str):
        """执行意图对应的命令"""
        intent_type = intent.intent_type
        params = intent.params

        # 音乐命令
        if intent_type in (CommandType.MUSIC_NEXT, CommandType.MUSIC_PREV,
                           CommandType.MUSIC_PAUSE, CommandType.MUSIC_RESUME,
                           CommandType.MUSIC_TOGGLE, CommandType.MUSIC_HIGHLIGHT,
                           CommandType.MUSIC_RESCAN):
            self.command_bus.execute(Command(
                command_type=intent_type,
                params=params,
                source="voice",
            ))

        elif intent_type == CommandType.MUSIC_VOLUME_SET:
            self.command_bus.execute(Command(
                command_type=intent_type,
                params=params,
                source="voice",
            ))

        elif intent_type == CommandType.MUSIC_VOLUME_UP:
            self.command_bus.execute(Command(
                command_type=CommandType.MUSIC_VOLUME_UP,
                params={"step": params.get("step", 5)},
                source="voice",
            ))

        elif intent_type == CommandType.MUSIC_VOLUME_DOWN:
            self.command_bus.execute(Command(
                command_type=CommandType.MUSIC_VOLUME_DOWN,
                params={"step": params.get("step", 5)},
                source="voice",
            ))

        elif intent_type == CommandType.MUSIC_SEARCH_PLAY:
            query = params.get("query", original_text)
            self.command_bus.execute(Command(
                command_type=CommandType.MUSIC_SEARCH_PLAY,
                params={"query": query},
                source="voice",
            ))

        # GameAlarm 命令
        elif intent_type == "alarm.flash":
            champion = params.get("champion", "")
            if champion:
                from ..lol.game_alarm import GameAlarmEngine
                # 通过事件触发
                self.event_bus.publish(Event(
                    event_type="alarm.create_flash",
                    data={"champion": champion},
                    source="voice"
                ))

        elif intent_type == "alarm.spell":
            champion = params.get("champion", "")
            spell = params.get("spell", "")
            if champion and spell:
                self.event_bus.publish(Event(
                    event_type="alarm.create_spell",
                    data={"champion": champion, "spell": spell},
                    source="voice"
                ))

        elif intent_type == "alarm.ward":
            location = params.get("location", "当前位置")
            self.event_bus.publish(Event(
                event_type="alarm.create_ward",
                data={"location": location},
                source="voice"
            ))

        elif intent_type == "alarm.custom":
            duration = params.get("duration", 120)
            title = params.get("title", "自定义提醒")
            self.event_bus.publish(Event(
                event_type="alarm.create_custom",
                data={"title": title, "duration": duration},
                source="voice"
            ))

        # 发布命令执行事件
        self.event_bus.publish(Event(
            event_type=EventType.VOICE_COMMAND_EXECUTED,
            data={"intent": intent.to_dict(), "original_text": original_text},
            source="voice"
        ))

    def set_asr_enabled(self, enabled: bool):
        """设置 ASR 是否启用"""
        self._asr_enabled = enabled
        self.state_store.set(StateNamespace.VOICE, "asr_enabled", enabled)

    def is_listening(self) -> bool:
        return self._listening
