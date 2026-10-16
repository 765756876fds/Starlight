"""
语音运行时 - 整合 ASR + Intent + 命令执行

负责：
- 麦克风音频采集（PTT 或持续监听）
- VAD 语音活动检测
- ASR 语音识别
- Intent 意图解析
- 命令执行（通过 CommandBus）
"""
import queue
import threading
import time
from typing import Any, Dict, Optional

from ..core.event_bus import EventBus, Event, EventType, get_event_bus
from ..core.command_bus import CommandBus, Command, CommandType, get_command_bus
from ..core.state_store import StateStore, StateNamespace, get_state_store

from .asr import ASRManager
from .intent import IntentRouter, Intent


def fold_duplicated_text(text: str) -> str:
    """折叠 ASR 输出中的重复拼接（含省略变体）。

    背景：播放声进麦克风导致 ASR 段变长/污染，llama-server 输出同一句
    重复 N 遍拼接（如 '播放电影X。播放X。播放电影X。' 4 遍）。
    这里只做轻量去重（不动 VAD 阈值/静音——会误杀电影在播时说话）。

    规则：按 。！？；切句；后续句与已保留句满足任一 → 丢弃（保留最先出现的完整版）：
      - 完全相同
      - 一方包含另一方
      - 共同前缀 >= 1 字 且 短者是长者按序子序列（省略变体重复，如 '播放X。' ⊂ '播放电影X。'）

    正常不同指令不受影响（'播放A。播放B。' 各自保留）。
    """
    import re
    parts = [p.strip() for p in re.findall(r"[^。！？；]+[。！？；]?", text)]
    if len(parts) <= 1:
        return text

    def _subseq(a, b):
        it = iter(b)
        return all(c in it for c in a)

    kept = []
    for p in parts:
        dup = False
        for k in kept:
            if p == k or p in k or k in p:
                dup = True
                break
            i = 0
            while i < len(p) and i < len(k) and p[i] == k[i]:
                i += 1
            if i >= 1:
                if len(p) < len(k) and _subseq(p, k):
                    dup = True
                    break
                if len(k) < len(p) and _subseq(k, p):
                    dup = True
                    break
        if not dup:
            kept.append(p)
    out = "".join(kept)
    return out if out.strip() else text


class VoiceRuntime:
    """
    语音运行时

    整合语音输入全流程。
    """

    def __init__(self, config: Dict[str, Any], tts_provider=None):
        self.config = config
        self.asr_config = config.get("asr", {})
        self.intent_config = config.get("intent", {})

        # 子系统
        self.asr = ASRManager(self.asr_config)
        self.intent_router = IntentRouter(self.intent_config)

        # TTS 状态提供者（回声抑制用；惰性调用，避免构造期依赖）
        # 约定：返回带 is_playing() 方法的对象，或 None（不启用回声抑制）
        self.tts_provider = tts_provider

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

        # 采集/识别解耦（场景 6/25）：
        # 采集线程（voice-mic）只读麦克风 + VAD → 语音段入有界队列；
        # 识别线程（voice-asr）串行消费队列 → ASR → Intent → 命令执行。
        # 有界队列满时丢最旧（用户最新指令优先）。
        self._speech_queue: "queue.Queue" = queue.Queue(maxsize=8)
        self._asr_thread: Optional[threading.Thread] = None
        # 代际标记：stop 后立即 start 时，旧识别线程（若 transcribe 卡住未及时退出）
        # 发现代际过期后自动退出 → 防止双消费者同时取队列。
        self._asr_generation = 0

    def start(self):
        """启动语音运行时"""
        if self._running:
            return

        self._running = True

        # 初始化 ASR（加载模型可能需要时间）
        print("[Voice] 正在加载 ASR 模型...")
        self.asr.initialize()

        # 采集/识别解耦：识别线程随 VoiceRuntime 生命周期（start 创建、stop 退出），
        # toggle_mic 只开关采集线程（识别线程常驻等待队列，无状态）。

        # ① 旧识别线程若仍存活（stop 后立即 start 的窗口）：等它退出。
        #    join 会阻塞本调用至多 1.0s（start 仅在启动链调用一次，可接受）；
        #    若超时未退出（如 transcribe 卡到 30s 超时），由代际标记兜底：
        #    旧线程下一轮循环发现 _asr_generation 过期 → 自动退出。
        if self._asr_thread is not None and self._asr_thread.is_alive():
            self._asr_thread.join(timeout=1.0)

        # ② 清空队列：丢弃 stop 后残留段 / 上次未处理段，确保新会话干净。
        #    （stop 排空后采集线程仍可能并发 put 一段在 sentinel 之后 → 此处清掉）
        while not self._speech_queue.empty():
            try:
                self._speech_queue.get_nowait()
            except queue.Empty:
                break

        # ③ 代际递增 + 创建新识别线程（捕获 my_gen；旧线程见代际过期自动退出）
        self._asr_generation += 1
        my_gen = self._asr_generation
        self._asr_thread = threading.Thread(
            target=self._asr_loop, args=(my_gen,), daemon=True, name="voice-asr"
        )
        self._asr_thread.start()

        # Intent 为纯规则实现，无状态，无需 initialize

        # 麦克风默认不启动，需要手动点击UI开关开启
        self._mic_on = False

        self.state_store.set(StateNamespace.VOICE, "enabled", True)
        print("[Voice] 运行时已启动（麦克风默认关闭）")

    def stop(self):
        """停止语音运行时"""
        self._running = False
        self._listening = False

        # 排空队列 + put sentinel：让识别线程处理完当前段后退出。
        # 采集线程可能仍在并发 put（stop 后新段落在 sentinel 之后）→
        # 这些残留段由下次 start() 清空，不会被本会话处理。
        while not self._speech_queue.empty():
            try:
                self._speech_queue.get_nowait()
            except queue.Empty:
                break
        self._speech_queue.put_nowait(None)  # sentinel

        self.asr.unload_all()
        print("[Voice] 运行时已停止")

    def _start_microphone(self):
        """启动麦克风采集线程"""
        self._mic_thread = threading.Thread(
            target=self._mic_loop, daemon=True, name="voice-mic"
        )
        self._mic_thread.start()

    def _stop_microphone(self):
        """停止麦克风采集"""
        self._listening = False

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
                        # 回声抑制：TTS 正在播放/生成，本段视为回声，丢弃不采集
                        if not self._should_collect_speech():
                            continue
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
                                self._enqueue_speech(b"".join(speech_frames), sample_rate)
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

    def _enqueue_speech(self, audio_data: bytes, sample_rate: int):
        """语音段入队（采集线程调用，永不阻塞）。

        队列满时丢最旧一段（FIFO 最前）→ 语义 = 用户最新指令优先。
        """
        item = (audio_data, sample_rate)
        try:
            self._speech_queue.put_nowait(item)
        except queue.Full:
            try:
                self._speech_queue.get_nowait()  # 丢最旧
            except queue.Empty:
                pass
            self._speech_queue.put_nowait(item)

    def _asr_loop(self, my_gen: int):
        """识别线程：串行消费队列 → ASR → Intent → 命令执行。

        - sentinel(None) → 退出（stop 流程）
        - 代际过期（stop 后立即 start 的残留旧线程，join 超时未退时兜底）
          → 下一轮循环自动退出，防止双消费者同时取队列（顺序乱 + 旧线程不退出）
        - 识别异常 try/except 兜底打印后继续：故障隔离，采集线程不受影响
        """
        while self._running and self._asr_generation == my_gen:
            try:
                seg = self._speech_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if seg is None:
                break
            try:
                self._process_speech(*seg)
            except Exception as e:
                print(f"[Voice] 识别线程异常: {e}")
        print(f"[Voice] 识别线程 gen={my_gen} 退出")

    def _should_collect_speech(self) -> bool:
        """回声抑制闸门：TTS 正在生成/播放期间，麦克风新语音段不采集。

        返回 False = 丢弃本段语音（不进入 ASR）。
        在 VAD 上升沿调用（回声到达麦克风时 TTS 大概率仍在播）。
        """
        try:
            tts = self.tts_provider() if self.tts_provider else None
        except Exception:
            tts = None
        if tts is not None and tts.is_playing():
            return False
        return True

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
        # 防御：上升沿闸门通过但识别开始时 TTS 仍在播（长播报场景）→ 丢弃
        if not self._should_collect_speech():
            return

        # ASR 识别
        _t0 = time.monotonic()
        text = self.asr.transcribe(audio_data, sample_rate)
        _t_asr = time.monotonic()
        if not text or not text.strip():
            return

        # 轻量去重：ASR 重复拼接（播放声污染场景）→ 只保留一段
        text = fold_duplicated_text(text)

        print(f"[Voice] 识别: {text}")

        # 发布识别结果事件
        self.event_bus.publish(Event(
            event_type=EventType.VOICE_TEXT_RECEIVED,
            data={"text": text},
            source="voice"
        ))

        # 意图解析
        intent = self.intent_router.parse(text)
        _t_intent = time.monotonic()
        print(f"[Voice] 意图: {intent}")

        # 发布意图事件
        self.event_bus.publish(Event(
            event_type=EventType.VOICE_INTENT_PARSED,
            data={"intent": intent.to_dict(), "original_text": text},
            source="voice"
        ))

        # 执行命令
        self._execute_intent(intent, text)
        _t_cmd = time.monotonic()
        print("[PERF] asr=%.0fms intent=%.0fms command=%.0fms total=%.0fms i=%s" % (
            (_t_asr-_t0)*1000, (_t_intent-_t_asr)*1000,
            (_t_cmd-_t_intent)*1000, (_t_cmd-_t0)*1000, intent.intent_type))

    # ---- 包2b：谁在播谁响应（电影/音乐暂停继续路由） ----
    # 路由读 state_store 的 MOVIE 播放态（movie_controller play/pause/stop 写入）。
    # 读不到/异常 → 按音乐处理（现状零变化）。

    def _movie_state(self):
        """读电影播放态：(playing=在播占位, paused=暂停中)。"""
        try:
            playing = bool(self.state_store.get(StateNamespace.MOVIE, "playing", False))
            paused = bool(self.state_store.get(StateNamespace.MOVIE, "paused", False))
            return playing, paused
        except Exception:
            return False, False

    def _route_pause_cmd(self):
        """暂停路由：电影在播（未暂停）→ MOVIE_PAUSE；否则 MUSIC_PAUSE。
        电影暂停中说"暂停" → 走音乐（音乐没播则 no-op，不报错）。"""
        playing, paused = self._movie_state()
        if playing and not paused:
            return CommandType.MOVIE_PAUSE
        return CommandType.MUSIC_PAUSE

    def _route_resume_cmd(self):
        """继续路由：电影在播（含暂停中）→ MOVIE_RESUME；否则 MUSIC_RESUME。"""
        playing, _ = self._movie_state()
        if playing:
            return CommandType.MOVIE_RESUME
        return CommandType.MUSIC_RESUME

    def _execute_intent(self, intent: Intent, original_text: str):
        """执行意图对应的命令"""
        intent_type = intent.intent_type
        params = intent.params

        # 音乐命令（暂停/继续：谁在播谁响应——电影在播时路由给电影，包2b）
        if intent_type == CommandType.MUSIC_PAUSE:
            self.command_bus.execute(Command(
                command_type=self._route_pause_cmd(),
                params=params,
                source="voice",
            ))

        elif intent_type == CommandType.MUSIC_RESUME:
            self.command_bus.execute(Command(
                command_type=self._route_resume_cmd(),
                params=params,
                source="voice",
            ))

        elif intent_type in (CommandType.MUSIC_NEXT, CommandType.MUSIC_PREV,
                             CommandType.MUSIC_TOGGLE, CommandType.MUSIC_HIGHLIGHT,
                             CommandType.MUSIC_CLEANUP,
                             CommandType.MUSIC_HIGHLIGHT_MODE,
                             CommandType.MUSIC_SET_PLAY_MODE,
                             CommandType.MUSIC_CLEAR_QUEUE):
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

        elif intent_type == CommandType.MOVIE_SEARCH_PLAY:
            # 长任务（在线搜索）：异步执行，不阻塞识别线程（与 MUSIC_SEARCH_PLAY 同策略）
            query = params.get("query", original_text)
            cmd_params = {"query": query}
            if params.get("episode") is not None:
                cmd_params["episode"] = params["episode"]
            self.command_bus.execute_async(Command(
                command_type=CommandType.MOVIE_SEARCH_PLAY,
                params=cmd_params,
                source="voice",
            ))

        elif intent_type == CommandType.MUSIC_SEARCH_PLAY:
            # 长任务（在线搜索/JS 桥接，10s+）：异步执行，不阻塞识别线程
            # （后续语音指令可立即处理）。
            # 已知行为：长任务异步后，后续短命令可能先完成。
            # 用户说话有间隔，实际并发概率低；若需严格保序，后续可引入命令队列。
            query = params.get("query", original_text)
            self.command_bus.execute_async(Command(
                command_type=CommandType.MUSIC_SEARCH_PLAY,
                params={"query": query},
                source="voice",
            ))

        elif intent_type == CommandType.MUSIC_RESCAN:
            # 长任务（重新扫描音乐库，大库可能数分钟）：异步执行，不阻塞识别线程。
            # 已知行为：长任务异步后，后续短命令可能先完成。
            self.command_bus.execute_async(Command(
                command_type=CommandType.MUSIC_RESCAN,
                params=params,
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
                data={"title": title, "duration": duration, "content": params.get("content", "")},
                source="voice"
            ))

        elif intent_type in ("alarm.list", "alarm.remaining", "alarm.cancel_last"):
            self.event_bus.publish(Event(
                event_type=intent_type,
                data=dict(params),
                source="voice"
            ))

        elif intent_type in ("cooldown.start", "cooldown.query",
                             "cooldown.cancel", "cooldown.cancel_all"):
            # 冷却计时 / 静态CD查询：转发给已在 main 注册的 CooldownManager
            self.command_bus.execute(Command(
                command_type=intent_type,
                params=params,
                source="voice",
            ))

        elif intent_type == CommandType.PLAYER_QUERY:
            # 长任务（查询对局/玩家信息，走网络）：异步执行，不阻塞识别线程。
            # 已知行为：长任务异步后，后续短命令可能先完成。
            self.command_bus.execute_async(Command(
                command_type=CommandType.PLAYER_QUERY,
                params=params,
                source="voice",
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

    def toggle_mic(self) -> bool:
        """开关麦克风，返回当前状态"""
        if self._mic_on:
            self._stop_microphone()
            self._mic_on = False
            print("[Voice] 麦克风已关闭")
        else:
            self._start_microphone()
            self._mic_on = True
            print("[Voice] 麦克风已开启")
        return self._mic_on

    def is_mic_on(self) -> bool:
        return getattr(self, "_mic_on", False)

