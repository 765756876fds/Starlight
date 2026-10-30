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
from typing import Any

from ..core.command_bus import Command, CommandType, get_command_bus
from ..core.event_bus import Event, EventType, get_event_bus
from ..core.state_store import StateNamespace, get_state_store
from .asr import ASRManager
from .intent import Intent, IntentRouter


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

    def __init__(self, config: dict[str, Any], tts_provider=None,
                 speech_queue_provider=None):
        self.config = config
        self.asr_config = config.get("asr", {})
        self.intent_config = config.get("intent", {})

        # 子系统
        self.asr = ASRManager(self.asr_config)
        self.intent_router = IntentRouter(self.intent_config)

        # TTS 状态提供者（回声抑制用；惰性调用，避免构造期依赖）
        # 约定：返回带 is_playing() 方法的对象，或 None（不启用回声抑制）
        self.tts_provider = tts_provider

        # SpeechQueue 提供者（system.stop_tts 独立打断路由用；惰性调用）
        # 约定：返回带 stop_current() 方法的对象，或 None（降级：只停 TTS）
        self.speech_queue_provider = speech_queue_provider

        # V2-2：全局暂停快照 {domain: {"gen": int}}，只记录 IPC 确认暂停成功的媒体
        self._global_pause_snapshot: dict[str, dict] = {}
        # V2-2：mpv 提供者（惰性，默认 None；main wiring 注入）
        self.music_mpv_provider = None
        self.movie_mpv_provider = None
        # V2-2：控制器提供者（取 _playback_gen）
        self.music_controller_provider = None
        self.movie_controller_provider = None

        # 打断旁路（Side-channel）参数：
        # TTS 播放期间采集的段走独立队列与独立检测线程（不挤占主 ASR 队列）。
        self._INTERRUPT_SLICE_SECONDS = 1.5  # 旁路只截取音频前 1.5s 识别（缩短耗时）
        # TTS 播放期间（is_interrupt 段）的静音切段阈值：10 帧 ≈ 0.64s（保留完整句子，
        # 不强行截断语音；正常段仍用 mic_loop 的 max_silence=30 帧 ≈ 1.92s）
        self._INTERRUPT_MAX_SILENCE = 10
        self._INTERRUPT_KEYWORDS = (
            "停止播放", "停止", "闭嘴", "安静", "安静点", "别说了", "别讲",
            "别播", "不要说了", "别吵", "停播", "不听了", "不想听了", "太吵了",
        )
        # 否定词拦截：文本含这些词 → 视为"不要停"，忽略不打断
        self._INTERRUPT_NEGATIONS = ("不要停", "不要停止", "别停", "别停止")
        # 旁路识别超时（秒）：2s 无结果即放弃，绝不卡死旁路线程
        self._INTERRUPT_TIMEOUT_SECONDS = 2.0

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
        self._mic_thread: threading.Thread | None = None

        # 采集/识别解耦（场景 6/25）：
        # 采集线程（voice-mic）只读麦克风 + VAD → 语音段入有界队列；
        # 识别线程（voice-asr）串行消费队列 → ASR → Intent → 命令执行。
        # 有界队列满时丢最旧（用户最新指令优先）。
        self._speech_queue: queue.Queue = queue.Queue(maxsize=8)
        self._asr_thread: threading.Thread | None = None
        # 代际标记：stop 后立即 start 时，旧识别线程（若 transcribe 卡住未及时退出）
        # 发现代际过期后自动退出 → 防止双消费者同时取队列。
        self._asr_generation = 0

        # 打断旁路队列（Side-channel）：TTS 播放期间采集的段（is_interrupt=True）
        # 绝不进主 ASR 队列，只进这里；offer_latest 语义（满丢旧塞新，最新打断意图优先）。
        self._interrupt_queue: queue.Queue = queue.Queue(maxsize=2)
        self._interrupt_thread: threading.Thread | None = None
        self._interrupt_stop = threading.Event()

    def start(self, bootstrap_result: str = "unknown"):
        """启动语音运行时。

        bootstrap_result:
          "ok"        — main.py 已确认 ASR 服务就绪，跳过重复 ensure_and_launch 和 30s 等待；
          "fail"      — main.py 已确认 ASR 不可用，跳过重复 bootstrap 和 30s 等待；
          "disabled"  — asr.enabled=false，完全不初始化 ASR（TTS/其他仍可用）；
          "unknown"   — 旧调用路径（未走 main.py initialize），保持原有行为。
        """
        if self._running:
            return

        self._running = True

        if bootstrap_result == "disabled":
            print("[Voice] asr.enabled=false，跳过 ASR 初始化（TTS 仍可用）")
            return

        # 初始化 ASR（加载模型可能需要时间）
        print("[Voice] 正在加载 ASR 模型...")
        self.asr.initialize(bootstrap_result=bootstrap_result)

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
        while not self._interrupt_queue.empty():
            try:
                self._interrupt_queue.get_nowait()
            except queue.Empty:
                break

        # ③ 代际递增 + 创建新识别线程（捕获 my_gen；旧线程见代际过期自动退出）
        self._asr_generation += 1
        my_gen = self._asr_generation
        self._asr_thread = threading.Thread(
            target=self._asr_loop, args=(my_gen,), daemon=True, name="voice-asr"
        )
        self._asr_thread.start()

        # ④ 打断旁路检测线程（独立于主 ASR 循环；stop 时经 _interrupt_stop 退出）
        self._interrupt_stop.clear()
        self._interrupt_thread = threading.Thread(
            target=self._interrupt_worker, daemon=True, name="voice-interrupt"
        )
        self._interrupt_thread.start()

        # Intent 为纯规则实现，无状态，无需 initialize

        # 麦克风默认不启动，需要手动点击UI开关开启
        self._mic_on = False

        self.state_store.set(StateNamespace.VOICE, "enabled", True)
        print("[Voice] 运行时已启动（麦克风默认关闭）")

    def stop(self):
        """停止语音运行时"""
        self._running = False
        self._listening = False

        # 排空主队列 + put sentinel：让识别线程处理完当前段后退出。
        # 采集线程可能仍在并发 put（stop 后新段落在 sentinel 之后）→
        # 这些残留段由下次 start() 清空，不会被本会话处理。
        while not self._speech_queue.empty():
            try:
                self._speech_queue.get_nowait()
            except queue.Empty:
                break
        self._speech_queue.put_nowait(None)  # sentinel

        # 退出打断旁路线程（Event 置位 → worker 下一轮退出；join 带超时）
        self._interrupt_stop.set()
        if self._interrupt_thread is not None and self._interrupt_thread.is_alive():
            self._interrupt_thread.join(timeout=1.0)
            self._interrupt_thread = None

        # 等主识别线程退出（可能正处理当前段；join 带超时，daemon 兜底）
        if self._asr_thread is not None and self._asr_thread.is_alive():
            self._asr_thread.join(timeout=1.0)

        # 收尾清空：识别线程可能因 _running=False 在消费 sentinel 前退出，
        # 残留 sentinel 一并清掉，保证 stop 后队列干净。
        while not self._speech_queue.empty():
            try:
                self._speech_queue.get_nowait()
            except queue.Empty:
                break

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
        except ImportError as e:
            import sys as _sys
            print(f"[Voice] pyaudio 导入失败: {e}")
            print(f"[Voice] 当前 Python: {_sys.executable}")
            print(f"[Voice] 请确认用项目 .venv 启动（.venv\\Scripts\\python.exe main.py），"
                  f"并执行: .venv\\Scripts\\pip install -r requirements.txt")
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
        is_interrupt = False  # 当前 VAD 段的打断标记（上升沿由 _speech_gate 决定）

        while self._running and self._listening:
            try:
                data = stream.read(chunk_size, exception_on_overflow=False)
                volume = self._calculate_volume(data)

                if volume > silence_threshold:
                    # 检测到语音
                    silence_duration = 0
                    if not in_speech:
                        # 回声抑制（放宽版）：TTS 播放中也允许采集，
                        # 但该段标记 is_interrupt（打断候选），走独立旁路。
                        collect, is_interrupt = self._speech_gate()
                        if not collect:
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

                        # TTS 播放期间（is_interrupt 段）缩短静音切段阈值：
                        # 10 帧 ≈ 0.64s 静音即切段（保留完整句子，无需强行截断；
                        # 正常段仍用 max_silence=30 帧 ≈ 1.92s）
                        effective_max_silence = self._INTERRUPT_MAX_SILENCE if is_interrupt else max_silence
                        if silence_duration >= effective_max_silence:
                            # 语音结束（min_speech 至少 5 帧 ≈ 0.32s 有效声音才认指令）
                            if speech_count >= min_speech:
                                self._enqueue_speech(b"".join(speech_frames), sample_rate, is_interrupt)
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

    def _enqueue_speech(self, audio_data: bytes, sample_rate: int,
                        is_interrupt: bool = False):
        """语音段入队（采集线程调用，永不阻塞）。

        is_interrupt=True（TTS 播放期间的段）→ 打断旁路队列 _interrupt_queue：
          - 绝不进主 ASR 队列（不挤占主 ASR，主 ASR 转写长语音时打断不被卡）；
          - offer_latest 语义：队列满丢旧塞新（最新打断意图优先）。
        is_interrupt=False → 主 ASR 队列（满丢最旧，用户最新指令优先）。
        """
        if is_interrupt:
            # 打点：入队时刻（worker 取到后计算排队耗时 [Interrupt-Perf] Queue）
            t_enter = time.monotonic()
            item = (audio_data, sample_rate, t_enter)
            try:
                self._interrupt_queue.put_nowait(item)
            except queue.Full:
                try:
                    self._interrupt_queue.get_nowait()  # 丢旧
                except queue.Empty:
                    pass
                try:
                    self._interrupt_queue.put_nowait(item)
                except queue.Full:
                    pass
            return

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

    def _speech_gate(self):
        """采集闸门（放宽版）：TTS 播放中允许采集，但标记打断候选。

        返回 (collect, is_interrupt)：
        - TTS 空闲 / 未注入 / 异常 → (True, False)：正常采集，不标记。
        - TTS 播放中 → (True, True)：不再"装聋"拒收，该段标记为打断候选；
          短促回声/杂音由 _process_speech 的时长过滤（<= 1.5s）丢弃，
          长段（可能是用户指令，如"停止播放"）照常送入 ASR 识别。
        """
        try:
            tts = self.tts_provider() if self.tts_provider else None
        except Exception:
            tts = None
        if tts is not None and tts.is_playing():
            return True, True
        return True, False

    # ===== 打断旁路（Side-channel）：独立队列 + 独立检测线程 =====

    def _interrupt_worker(self):
        """打断旁路检测线程（独立于主 _asr_loop）。

        从 _interrupt_queue 取段（offer_latest 已保证队列里是最新打断意图），
        截取前 1.5s 音频做无锁识别；文本含打断关键词 → 立即 _stop_tts_now()。
        - 绝不阻塞主 ASR 的锁（见 _transcribe_interrupt：直调无锁引擎，绕开 ASRManager._lock）；
        - 主 ASR 正在转写长语音时，本线程照常取队列并发检测，打断不被排队卡住。
        """
        while self._running and not self._interrupt_stop.is_set():
            try:
                audio_data, sample_rate, t_enter = self._interrupt_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            t_start = time.monotonic()
            try:
                self._detect_interrupt(audio_data, sample_rate)
            except Exception as e:
                print(f"[Voice] 打断检测异常: {e}")
            t_end = time.monotonic()
            # 延迟打点：排队耗时（入队→worker 取到）与识别耗时（请求→结果）
            print(f"[Interrupt-Perf] Queue: {t_start - t_enter:.2f}s, ASR: {t_end - t_start:.2f}s")
        print("[Voice] 打断旁路线程退出")

    def _detect_interrupt(self, audio_data: bytes, sample_rate: int):
        """识别一段打断候选音频，命中打断词 → 停止 TTS。"""
        # 截取前 1.5s（打断词通常在句首；缩短识别耗时）
        slice_bytes = int(self._INTERRUPT_SLICE_SECONDS * sample_rate * 2)
        audio = audio_data[:slice_bytes] if len(audio_data) > slice_bytes else audio_data
        text = self._transcribe_interrupt(audio, sample_rate)
        if not text:
            return
        # 否定词拦截（必须在关键词匹配之前）："不要停/别停止"等 → 视为不打断
        for neg in self._INTERRUPT_NEGATIONS:
            if neg in text:
                print(f"[Voice] 打断旁路否定词命中: {text!r} → 忽略")
                return
        for kw in self._INTERRUPT_KEYWORDS:
            if kw in text:
                print(f"[Voice] 打断旁路命中: {text!r} → 停止 TTS")
                self._stop_tts_now()
                return

    def _transcribe_interrupt(self, audio_data: bytes, sample_rate: int) -> str:
        """无锁转写：直调 active 引擎，绕开 ASRManager._lock。

        ASRManager.transcribe 全程持锁（锁内做完整 HTTP 转写），旁路若走它
        会在主 ASR 转写长语音时被卡住 → 这里直接读 _active_engine 并调引擎
        自己的 transcribe（Qwen3ServerASR 纯 HTTP、无内部锁，可与主 ASR 并发
        请求 llama-server）。有内部锁的引擎（本地 llama 模型）跳过旁路识别，
        绝不与主 ASR 抢锁。
        """
        mgr = self.asr
        engine = getattr(mgr, "_active_engine", None)
        if engine is None or not engine.is_available():
            return ""
        # 本地 llama 模型引擎（Qwen3ASR，带模型级锁）跳过旁路识别，
        # 绝不与主 ASR 抢锁。HTTP 引擎（Qwen3ServerASR）无锁，可旁路并发。
        if type(engine).__name__ == "Qwen3ASR":
            return ""
        try:
            # 强制独立超时：2s 无结果即放弃，绝不卡死旁路线程
            t0 = time.monotonic()
            try:
                return engine.transcribe(audio_data, sample_rate,
                                         timeout=self._INTERRUPT_TIMEOUT_SECONDS)
            finally:
                # HTTP 端到端总耗时打点（成功/失败都记录）
                print(f"[Interrupt-Perf] HTTP total: {time.monotonic() - t0:.2f}s")
        except Exception as e:
            print(f"[Voice] 旁路转写失败: {e}")
            return ""

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
        """处理一段语音（主 ASR 链：转写 → 意图 → 命令执行）。

        打断候选段的时长过滤已移除：TTS 播放期间的段走独立旁路
        （_interrupt_queue + _interrupt_worker），主队列不再区分。
        """
        if not self._asr_enabled:
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

    def _real_paused(self, provider) -> bool | None:
        """V2-2：实时 IPC 查 mpv pause 属性。True=暂停, False=在播, None=未知。"""
        if provider is None:
            print("[Voice] _real_paused: provider=None")
            return None
        try:
            mpv = provider()
            if mpv is None:
                print("[Voice] _real_paused: mpv instance None")
                return None
            if not mpv.is_running():
                print("[Voice] _real_paused: mpv process not running")
                return None
            v = mpv.get_property("pause", timeout=1.0)
            if v is None:
                print("[Voice] _real_paused: get_property pause returned None (IPC timeout/no data/busy)")
                return None
            return bool(v)
        except Exception as e:
            print(f"[Voice] _real_paused exception: {e!r}")
            return None

    def _enqueue_speak(self, text: str, category: str = "voice"):
        try:
            sq = self.speech_queue_provider() if self.speech_queue_provider else None
            if sq is None:
                return
            from app.speech.speech_queue import SpeechItem, PRIORITY_B
            import time as _t
            sq.enqueue(SpeechItem(text=text, priority=PRIORITY_B,
                                  expire_at=_t.time() + 30.0,
                                  category=category, interruptible=True))
        except Exception as e:
            print(f"[Voice] _enqueue_speak failed: {e}")

    def _global_pause(self):
        """V2-2：裸"暂停"→ 暂停所有实际在播的媒体，实时 IPC 确认后写快照。"""
        self._global_pause_snapshot = {}
        msgs = []
        # 音乐：用实时 IPC 判断是否在播
        music_paused_now = self._real_paused(getattr(self, "music_mpv_provider", None))
        if music_paused_now is False:  # pause=False = 正在播放
            try:
                self.command_bus.execute(Command(command_type=CommandType.MUSIC_PAUSE,
                                                params={}, source="voice"))
                import time as _t; _t.sleep(0.2)
                paused = self._real_paused(getattr(self, "music_mpv_provider", None))
                if paused is True:
                    try:
                        mc = self.music_controller_provider() if self.music_controller_provider else None
                        gen = getattr(mc, "_playback_gen", None) if mc else None
                    except Exception:
                        gen = None
                    if isinstance(gen, int):
                        self._global_pause_snapshot["music"] = {"gen": gen}
                        msgs.append("音乐已暂停")
                    else:
                        msgs.append("音乐已暂停，但会话标识不可用")
                elif paused is False:
                    msgs.append("音乐暂停失败")
                else:
                    msgs.append("音乐暂停状态无法确认")
            except Exception as e:
                print(f"[Voice] 全局暂停音乐失败: {e}")
                msgs.append("音乐暂停失败")
        # 电影：用实时 IPC 判断是否在播（state_store 仅辅助）
        mv_paused_now = self._real_paused(getattr(self, "movie_mpv_provider", None))
        if mv_paused_now is False:  # pause=False = 正在播放
            try:
                self.command_bus.execute(Command(command_type=CommandType.MOVIE_PAUSE,
                                                 params={}, source="voice"))
                import time as _t; _t.sleep(0.2)
                paused = self._real_paused(getattr(self, "movie_mpv_provider", None))
                if paused is True:
                    try:
                        mv = self.movie_controller_provider() if self.movie_controller_provider else None
                        gen = getattr(mv, "_playback_gen", None) if mv else None
                    except Exception:
                        gen = None
                    if isinstance(gen, int):
                        self._global_pause_snapshot["movie"] = {"gen": gen}
                        msgs.append("电影已暂停")
                    else:
                        msgs.append("电影已暂停，但会话标识不可用")
                elif paused is False:
                    msgs.append("电影暂停失败")
                else:
                    msgs.append("电影暂停状态无法确认")
            except Exception as e:
                print(f"[Voice] 全局暂停电影失败: {e}")
                msgs.append("电影暂停失败")
        if not msgs:
            msgs.append("没有正在播放的媒体")
        try:
            sq = self.speech_queue_provider() if self.speech_queue_provider else None
            if sq is not None:
                from app.speech.speech_queue import SpeechItem, PRIORITY_B
                import time as _t2
                sq.enqueue(SpeechItem(text="，".join(msgs), priority=PRIORITY_B,
                                      expire_at=_t2.time() + 30.0,
                                      category="voice_global_pause", interruptible=True))
        except Exception as e:
            print(f"[Voice] 全局暂停反馈失败: {e}")

    def _global_resume(self):
        """V2-2：裸"继续"→ 只恢复本次全局暂停确认过的媒体。"""
        if not self._global_pause_snapshot:
            try:
                sq = self.speech_queue_provider() if self.speech_queue_provider else None
                if sq is not None:
                    from app.speech.speech_queue import SpeechItem, PRIORITY_B
                    import time as _t
                    sq.enqueue(SpeechItem(text="当前没有可恢复的歌曲，请说出歌名，或先选择一首歌曲", priority=PRIORITY_B,
                                          expire_at=_t.time() + 30.0,
                                          category="voice_global_resume", interruptible=True))
            except Exception:
                pass
            return
        msgs = []
        movie_attempted = False
        music_attempted = False
        for domain in ("movie", "music"):
            entry = self._global_pause_snapshot.get(domain)
            if not entry:
                continue
            try:
                expected_gen = entry.get("gen", None)
                if expected_gen is None:
                    msgs.append(f"{domain} 快照无效，跳过")
                    continue
                if domain == "music":
                    mc = self.music_controller_provider() if self.music_controller_provider else None
                    if mc is None:
                        msgs.append("音乐控制器不可用，跳过")
                        continue
                    cur_gen = getattr(mc, "_playback_gen", None)
                else:
                    mv = self.movie_controller_provider() if self.movie_controller_provider else None
                    if mv is None:
                        msgs.append("电影控制器不可用，跳过")
                        continue
                    cur_gen = getattr(mv, "_playback_gen", None)
                if cur_gen != expected_gen:
                    msgs.append(f"{domain} 已切换，跳过恢复")
                    continue
                provider = self.music_mpv_provider if domain == "music" else self.movie_mpv_provider
                paused = self._real_paused(provider)
                if paused is False:
                    msgs.append(f"{domain} 已在播放，无需恢复")
                    continue
                if paused is None:
                    msgs.append(f"{domain} 状态无法确认，跳过")
                    continue
                cmd = CommandType.MUSIC_RESUME if domain == "music" else CommandType.MOVIE_RESUME
                self.command_bus.execute(Command(command_type=cmd, params={}, source="voice"))
                if domain == "music":
                    music_attempted = True
                else:
                    movie_attempted = True
            except Exception as e:
                print(f"[Voice] 全局恢复 {domain} 失败: {e}")
                msgs.append(f"{domain} 恢复失败")
        # 命令执行后实时复查最终态（音乐 MUSIC_PLAY 互斥同步暂停电影）
        import time as _t; _t.sleep(0.2)
        if music_attempted:
            music_paused_now = self._real_paused(getattr(self, "music_mpv_provider", None))
            if music_paused_now is False:
                msgs.append("音乐已恢复")
            elif music_paused_now is True:
                msgs.append("音乐恢复后仍处于暂停")
            else:
                msgs.append("音乐恢复状态无法确认")
            # 音乐恢复后电影最终态（仅当电影曾在快照或被互斥影响时播报）
            movie_in_snap = "movie" in self._global_pause_snapshot
            if movie_in_snap:
                movie_paused_now = self._real_paused(getattr(self, "movie_mpv_provider", None))
                if movie_paused_now is True:
                    msgs.append("电影因音乐优先保持暂停")
                elif movie_paused_now is False:
                    msgs.append("电影已恢复")
                else:
                    msgs.append("电影状态无法确认")
        else:
            if movie_attempted:
                movie_paused_now = self._real_paused(getattr(self, "movie_mpv_provider", None))
                if movie_paused_now is False:
                    msgs.append("电影已恢复")
                elif movie_paused_now is True:
                    msgs.append("电影恢复后仍处于暂停")
                else:
                    msgs.append("电影状态无法确认")
        self._global_pause_snapshot = {}
        try:
            sq = self.speech_queue_provider() if self.speech_queue_provider else None
            if sq is not None:
                from app.speech.speech_queue import SpeechItem, PRIORITY_B
                import time as _t
                sq.enqueue(SpeechItem(text="，".join(msgs), priority=PRIORITY_B,
                                      expire_at=_t.time() + 30.0,
                                      category="voice_global_resume", interruptible=True))
        except Exception as e:
            print(f"[Voice] 全局恢复反馈失败: {e}")

    def _stop_tts_now(self):
        """立即停止 TTS 播报（独立打断路由，不走 CommandBus）。

        1) TTSController.stop_playback()：物理停止 mpv 播放（异步内部，瞬间返回）；
        2) SpeechQueue.stop_current()：标记当前播报项 cancelled + 通知队列收尾。
        两个提供者均为惰性可选的：缺省时降级为"只停 TTS"或 no-op，
        任何异常都只打日志，绝不影响识别线程。
        """
        tts = None
        try:
            tts = self.tts_provider() if self.tts_provider else None
        except Exception:
            tts = None
        if tts is not None:
            try:
                tts.stop_playback()
            except Exception as e:
                print(f"[Voice] 停止 TTS 失败: {e}")

        sq = None
        try:
            sq = self.speech_queue_provider() if self.speech_queue_provider else None
        except Exception:
            sq = None
        if sq is not None:
            try:
                sq.stop_current()
            except Exception as e:
                print(f"[Voice] 停止语音队列失败: {e}")

    def _execute_intent(self, intent: Intent, original_text: str):
        """执行意图对应的命令"""
        intent_type = intent.intent_type
        params = intent.params

        # 独立打断路由（system.stop_tts）：不走 CommandBus，
        # 直接停止 TTS 播报（物理停止 + 队列当前项标记取消），
        # 保证语音打断即时生效且不经过业务中间件。
        if intent_type == "system.stop_tts":
            self._stop_tts_now()

        # V2-2：裸"暂停"→ 全局暂停所有在播媒体；裸"继续"→ 只恢复本次确认暂停的媒体
        elif intent_type == CommandType.MUSIC_PAUSE:
            self._global_pause()

        elif intent_type == CommandType.MUSIC_RESUME:
            self._global_resume()

        elif intent_type == CommandType.MUSIC_PLAY:
            try:
                ok = self.command_bus.execute(Command(
                    command_type=intent_type,
                    params=params,
                    source="voice",
                ))
            except Exception as e:
                print(f"[Voice] {intent_type} error: {e}")
                ok = False
            self._enqueue_speak(
                "已开始播放本地歌曲" if ok else
                "本地没有可播放的歌曲，请手动选择歌曲，或直接说出歌名播放",
                "voice_music_play",
            )

        elif intent_type in (CommandType.AUTO_QUEUE_SET, CommandType.AUTO_ACCEPT_SET,
                             CommandType.AUTO_BANPICK_SET, CommandType.AUTO_COMBO_SET):
            try:
                ok = self.command_bus.execute(Command(
                    command_type=intent_type,
                    params={"enabled": bool(params.get("enabled"))},
                    source="voice",
                ))
            except Exception as e:
                print(f"[Voice] {intent_type} error: {e}")
                ok = False
            enabled = bool(params.get("enabled"))
            if not ok:
                message = "自动化操作未成功"
            elif not enabled:
                message = "自动化已关闭"
            elif isinstance(ok, dict) and not ok.get("client_ready", False):
                message = "自动化开关已开启，等待游戏连接"
            else:
                message = "自动化开关已开启"
            self._enqueue_speak(message, "voice_automation")

        elif intent_type == CommandType.AUTO_STATUS_QUERY:
            try:
                result = self.command_bus.execute(Command(
                    command_type=intent_type, params=params, source="voice"))
            except Exception as e:
                print(f"[Voice] {intent_type} error: {e}")
                result = False
            labels = {"queue": "自动排队", "accept": "自动同意",
                      "banpick": "自动禁用和选择英雄", "combo": "自动连招"}
            label = labels.get(params.get("feature"), "游戏自动化")
            if not isinstance(result, dict) or not result.get("available"):
                message = f"暂时无法确认{label}状态"
            elif result.get("enabled") and result.get("client_ready") is False:
                message = f"{label}已开启，但游戏客户端当前未连接"
            elif result.get("enabled"):
                message = f"{label}已开启"
            else:
                message = f"{label}已关闭"
            self._enqueue_speak(message, "voice_automation_status")

        elif intent_type in (CommandType.MUSIC_SLEEP_TIMER_SET,
                             CommandType.MUSIC_SLEEP_TIMER_CANCEL):
            if intent_type == CommandType.MUSIC_SLEEP_TIMER_SET and "seconds" not in params:
                self._enqueue_speak("请说出睡眠定时的时长，例如三十分钟", "voice_music_timer")
                return
            if intent_type == CommandType.MUSIC_SLEEP_TIMER_SET and params.get("seconds") not in (1800, 3600, 5400):
                self._enqueue_speak("目前支持三十分钟、六十分钟或九十分钟", "voice_music_timer")
                return
            try:
                ok = self.command_bus.execute(Command(
                    command_type=intent_type, params=params, source="voice"))
            except Exception as e:
                print(f"[Voice] {intent_type} error: {e}")
                ok = False
            self._enqueue_speak("睡眠定时已设置" if ok and intent_type == CommandType.MUSIC_SLEEP_TIMER_SET
                                else "睡眠定时已取消" if ok else "睡眠定时操作未成功",
                                "voice_music_timer")

        # 明确电影命令：用户明说"暂停电影"/"继续电影"，直接分发，不走谁在播路由
        elif intent_type in (CommandType.MOVIE_PAUSE, CommandType.MOVIE_RESUME, CommandType.MOVIE_STOP,
                             CommandType.MOVIE_VOLUME_SET, CommandType.MOVIE_VOLUME_UP, CommandType.MOVIE_VOLUME_DOWN,
                             CommandType.MOVIE_SEEK):
            try:
                ok = self.command_bus.execute(Command(
                    command_type=intent_type,
                    params=params,
                    source="voice",
                ))
            except Exception as e:
                print(f"[Voice] {intent_type} error: {e}")
                ok = False
            if ok is False:
                self._enqueue_speak("电影操作未成功", "voice_movie_control")

        elif intent_type == CommandType.PLAYER_NEXT:
            # V2-4.3：裸"下一个"上下文仲裁：仅当一边明确在播、另一边明确暂停才执行。
            m_paused = self._real_paused(getattr(self, "movie_mpv_provider", None))
            s_paused = self._real_paused(getattr(self, "music_mpv_provider", None))
            if m_paused is False and s_paused is True:
                self.command_bus.execute(Command(command_type=CommandType.MOVIE_NEXT_EPISODE, params=params, source="voice"))
            elif s_paused is False and m_paused is True:
                self.command_bus.execute(Command(command_type=CommandType.MUSIC_NEXT, params=params, source="voice"))
            # 其他情况（None/都暂停/都在播）静默不执行

        elif intent_type in (CommandType.MOVIE_NEXT_EPISODE, CommandType.MOVIE_PREV_EPISODE):
            try:
                ok = self.command_bus.execute(Command(
                    command_type=intent_type,
                    params=params,
                    source="voice",
                ))
            except Exception as e:
                print(f"[Voice] {intent_type} error: {e}")
                ok = False
            if ok is False:
                self._enqueue_speak("电影切集未成功", "voice_movie_episode")

        elif intent_type in (CommandType.MUSIC_NEXT, CommandType.MUSIC_PREV,
                             CommandType.MUSIC_TOGGLE, CommandType.MUSIC_HIGHLIGHT,
                             CommandType.MUSIC_CLEANUP,
                             CommandType.MUSIC_HIGHLIGHT_MODE,
                             CommandType.MUSIC_SET_PLAY_MODE,
                             CommandType.MUSIC_SET_SPEED,
                             CommandType.MUSIC_CLEAR_QUEUE,
                             CommandType.MUSIC_STOP,
                             CommandType.MUSIC_SEEK,
                             CommandType.MUSIC_IDENTIFY) or intent_type == CommandType.MUSIC_VOLUME_SET:
            self.command_bus.execute(Command(
                command_type=intent_type,
                params=params,
                source="voice",
            ))

        elif intent_type in (CommandType.MUSIC_PLAY_PLAYLIST,
                             CommandType.MUSIC_CREATE_PLAYLIST,
                             CommandType.MUSIC_ADD_TO_PLAYLIST,
                             CommandType.MUSIC_ADD_NEXT,
                             CommandType.MUSIC_PLAY_RANK_INDEX):
            try:
                ok = self.command_bus.execute(Command(
                    command_type=intent_type,
                    params=params,
                    source="voice",
                ))
            except Exception as e:
                print(f"[Voice] {intent_type} error: {e}")
                ok = False
            if ok:
                self._enqueue_speak("好的", "voice")
            else:
                self._enqueue_speak("操作未成功", "voice")

        elif intent_type == CommandType.MUSIC_SET_SPEED:
            if "speed" not in params or params.get("speed") is None:
                self._enqueue_speak("请说出要设置的倍速，例如一点五倍", "voice_music_speed")
                return
            ok = self.command_bus.execute(Command(
                command_type=intent_type,
                params=params,
                source="voice",
            ))
            if not ok:
                self._enqueue_speak("暂不支持该倍速，请使用支持的档位", "voice")

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
            try:
                ok = self.command_bus.execute(Command(
                    command_type=intent_type,
                    params=params,
                    source="voice",
                ))
            except Exception as e:
                print(f"[Voice] {intent_type} error: {e}")
                ok = False
            if intent_type == "cooldown.start":
                self._enqueue_speak("技能记录成功" if ok else "技能记录未成功",
                                    "voice_cooldown")
            elif intent_type == "cooldown.cancel":
                self._enqueue_speak("技能计时已取消" if ok else "没有找到要取消的技能计时",
                                    "voice_cooldown")
            elif intent_type == "cooldown.cancel_all":
                self._enqueue_speak(
                    f"已取消全部技能计时，共{ok}个" if isinstance(ok, int) and ok > 0
                    else "当前没有正在进行的技能计时",
                    "voice_cooldown")

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

