"""
VOICE-ECHO-01: 采集闸门 + 打断旁路（Side-channel）测试

闸门场景（放宽版）：
① TTS 正在播放（is_playing=True）→ _speech_gate 返回 (True, True)
   ——不再"装聋"拒收，允许采集并标记为打断候选（is_interrupt）
② TTS 空闲（is_playing=False）→ (True, False) 正常采集
③ 未注入 tts_provider（None）→ (True, False)
④ tts_provider 抛异常 → (True, False)（TTS 异常不卡语音链）

旁路队列与检测线程：
⑤ is_interrupt 段 → 只进 _interrupt_queue（绝不进主 ASR 队列）
⑥ _interrupt_queue 满（maxsize=2）→ offer_latest：丢旧塞新，队列保持最新意图
⑦ is_interrupt=False 段 → 只进主 _speech_queue
⑧ _detect_interrupt：无锁引擎识别出打断词（停/别/闭嘴…）→ 调 _stop_tts_now
⑨ _detect_interrupt：无打断词 → 不触发
⑩ _detect_interrupt：有锁引擎 → 旁路跳过（不抢主 ASR 锁）
⑪ 旁路只截取音频前 1.5s 识别（缩短耗时）
"""
from unittest.mock import MagicMock

from app.voice.runtime import VoiceRuntime


def _make_runtime(tts_provider=None):
    return VoiceRuntime({"asr": {}, "intent": {}}, tts_provider=tts_provider)


# ===== 采集闸门 =====

def test_interrupt_marked_when_tts_playing():
    tts = MagicMock()
    tts.is_playing.return_value = True
    runtime = _make_runtime(tts_provider=lambda: tts)
    assert runtime._speech_gate() == (True, True)


def test_collect_normal_when_tts_idle():
    tts = MagicMock()
    tts.is_playing.return_value = False
    runtime = _make_runtime(tts_provider=lambda: tts)
    assert runtime._speech_gate() == (True, False)


def test_no_provider_defaults_collect():
    runtime = _make_runtime(tts_provider=None)
    assert runtime._speech_gate() == (True, False)


def test_provider_exception_returns_collect():
    def boom():
        raise RuntimeError("tts provider down")
    runtime = _make_runtime(tts_provider=boom)
    assert runtime._speech_gate() == (True, False)


# ===== 旁路队列路由 =====

def test_interrupt_segment_goes_to_side_queue():
    """is_interrupt 段 → 只进 _interrupt_queue，绝不进主 ASR 队列。"""
    rt = _make_runtime()
    rt._enqueue_speech(b"\x00" * 3200, 16000, is_interrupt=True)
    assert rt._speech_queue.empty()
    assert rt._interrupt_queue.qsize() == 1
    audio, sr = rt._interrupt_queue.get_nowait()
    assert sr == 16000
    assert len(audio) == 3200


def test_interrupt_side_queue_offer_latest():
    """_interrupt_queue 满（maxsize=2）→ 丢旧塞新：最旧被丢，保留最近两条。"""
    rt = _make_runtime()
    rt._enqueue_speech(b"\x01" * 1600, 16000, is_interrupt=True)  # 最旧
    rt._enqueue_speech(b"\x02" * 1600, 16000, is_interrupt=True)
    rt._enqueue_speech(b"\x03" * 1600, 16000, is_interrupt=True)  # 最新
    assert rt._interrupt_queue.qsize() == 2
    got = [rt._interrupt_queue.get_nowait()[0] for _ in range(2)]
    assert b"\x01" * 1600 not in got   # 最旧被丢
    assert b"\x02" * 1600 in got
    assert b"\x03" * 1600 in got


def test_normal_segment_goes_to_main_queue():
    """is_interrupt=False → 只进主 _speech_queue，旁路队列不受影响。"""
    rt = _make_runtime()
    rt._enqueue_speech(b"\x00" * 1600, 16000, is_interrupt=False)
    assert rt._interrupt_queue.empty()
    assert rt._speech_queue.qsize() == 1


# ===== 旁路检测 =====

def _make_detect_runtime():
    rt = _make_runtime()
    rt._stop_tts_now = MagicMock()
    return rt


def test_detect_interrupt_keyword_triggers_stop():
    """无锁引擎识别出明确打断词 → 立即 _stop_tts_now。"""
    rt = _make_detect_runtime()
    engine = MagicMock()
    engine.transcribe.return_value = "停止播放"
    rt.asr._active_engine = engine
    rt._detect_interrupt(b"\x00" * 48000, 16000)
    rt._stop_tts_now.assert_called_once()
    engine.transcribe.assert_called_once()


def test_detect_interrupt_explicit_words_trigger():
    """明确指令词（闭嘴/安静/别说了）都触发打断。"""
    for kw_text in ("闭嘴", "安静", "别说了", "停止"):
        rt = _make_detect_runtime()
        engine = MagicMock()
        engine.transcribe.return_value = kw_text
        rt.asr._active_engine = engine
        rt._detect_interrupt(b"\x00" * 48000, 16000)
        rt._stop_tts_now.assert_called_once()


def test_detect_interrupt_single_char_no_trigger():
    """单字"停"/"别"不再触发（防误触：日常语流高频出现）。"""
    for kw_text in ("停", "别"):
        rt = _make_detect_runtime()
        engine = MagicMock()
        engine.transcribe.return_value = kw_text
        rt.asr._active_engine = engine
        rt._detect_interrupt(b"\x00" * 48000, 16000)
        rt._stop_tts_now.assert_not_called()


def test_detect_interrupt_negation_ignored():
    """否定词拦截：不要停/别停止（即使含"停止播放"关键词）→ 不打断。"""
    for text in ("不要停", "别停", "别停止播放", "不要停止播放", "别停止"):
        rt = _make_detect_runtime()
        engine = MagicMock()
        engine.transcribe.return_value = text
        rt.asr._active_engine = engine
        rt._detect_interrupt(b"\x00" * 48000, 16000)
        rt._stop_tts_now.assert_not_called()


def test_detect_interrupt_no_keyword_no_stop():
    """无打断词 → 不触发停止。"""
    rt = _make_detect_runtime()
    engine = MagicMock()
    engine.transcribe.return_value = "今天天气不错"
    rt.asr._active_engine = engine
    rt._detect_interrupt(b"\x00" * 48000, 16000)
    rt._stop_tts_now.assert_not_called()


def test_transcribe_interrupt_forces_timeout():
    """旁路转写强制 timeout=2.0（防阻塞：2s 无结果即放弃）。"""
    rt = _make_detect_runtime()
    engine = MagicMock()
    engine.transcribe.return_value = "停止播放"
    rt.asr._active_engine = engine
    rt._transcribe_interrupt(b"\x00" * 48000, 16000)
    engine.transcribe.assert_called_once()
    kwargs = engine.transcribe.call_args.kwargs
    assert kwargs.get("timeout") == 2.0
    rt._stop_tts_now.assert_not_called()  # 只测转写层，不触发停止


def test_detect_interrupt_locked_engine_skipped():
    """本地 llama 模型引擎（Qwen3ASR，带模型级锁）→ 旁路跳过，绝不抢主 ASR 锁。"""
    from app.voice.asr import Qwen3ASR
    rt = _make_detect_runtime()
    locked = Qwen3ASR(model_path="unused", mmproj_path="unused")
    locked.is_available = lambda: True  # 假装已加载，验证类型检查拦截
    rt.asr._active_engine = locked
    rt._detect_interrupt(b"\x00" * 48000, 16000)
    rt._stop_tts_now.assert_not_called()


def test_detect_interrupt_slices_first_seconds():
    """旁路只截取音频前 1.5s 识别（缩短耗时）。"""
    rt = _make_detect_runtime()
    engine = MagicMock()
    engine.transcribe.return_value = "别说了"
    rt.asr._active_engine = engine
    # 10s 音频 @16kHz mono = 320000 bytes；前 1.5s = 48000 bytes
    long_audio = b"\x00" * 320000
    rt._detect_interrupt(long_audio, 16000)
    # 传给引擎的音频长度 <= 1.5s 截取上限
    assert engine.transcribe.call_count == 1
    sent_audio = engine.transcribe.call_args[0][0]
    assert len(sent_audio) <= int(1.5 * 16000 * 2)
    rt._stop_tts_now.assert_called_once()
