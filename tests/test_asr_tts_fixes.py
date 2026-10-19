"""ASR load 重试 + TTS 403 重试退避测试。

覆盖场景：
- load(retry_seconds>0)：/models 先 503 后 200 → 重试成功
- load(retry_seconds>0)：一直 503 → 超时返回失败（_loaded=False）
- load() 默认（转写路径）：单次探测，503 即败，不阻塞线程
- TTS 403：两次 403 后成功（0s/1s/3s 退避），输出文件有效
- TTS 一直 403：3 次后放弃返回 None，且残留文件被清理
"""
import os
import time

from unittest import mock
from unittest.mock import AsyncMock, MagicMock


class FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


# ---------- 改动 2：ASR load 重试 ----------

def _engine():
    from app.voice.asr import Qwen3ServerASR
    return Qwen3ServerASR(base_url="http://127.0.0.1:8080/v1")


def test_load_retries_until_200():
    """/models 先 503 后 200 → load(retry_seconds>0) 重试成功"""
    engine = _engine()
    with mock.patch("requests.get") as m, mock.patch("time.sleep"):
        m.side_effect = [FakeResp(503), FakeResp(200)]
        engine.load(retry_seconds=10.0)
    assert engine._loaded is True
    assert m.call_count == 2


def test_load_gives_up_after_timeout():
    """一直 503 → 重试超时返回失败（_loaded=False）。

    不 mock time.sleep：重试等待必须真实推进单调钟，deadline 才会到达（探测1(0s) →
    sleep2 → 探测2(2.0s 超时) → 结束）。
    """
    engine = _engine()
    with mock.patch("requests.get") as m:
        m.side_effect = [FakeResp(503)] * 10
        engine.load(retry_seconds=2.0)
    assert engine._loaded is False
    assert m.call_count == 2


def test_load_single_probe_by_default():
    """转写路径：默认 retry_seconds=0 → 单次探测，503 即败，不阻塞"""
    engine = _engine()
    with mock.patch("requests.get") as m:
        m.return_value = FakeResp(503)
        engine.load()
    assert engine._loaded is False
    assert m.call_count == 1


def test_transcribe_custom_timeout():
    """transcribe(timeout=...)：旁路可传独立短超时（如 2s），默认用实例 30s。

    打断旁路 _transcribe_interrupt 强制 timeout=2.0 → 2s 无结果即放弃，
    绝不卡死旁路线程；主 ASR 路径不传 → 仍用实例默认超时。
    """
    from app.voice.asr import Qwen3ServerASR
    engine = Qwen3ServerASR(base_url="http://127.0.0.1:8080/v1")
    engine._loaded = True
    audio = b"\x00" * 3200
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"text": "<asr_text>停止播放</asr_text>"}

    with mock.patch("requests.post", return_value=resp) as m:
        out = engine.transcribe(audio, 16000, timeout=2.0)
        assert m.call_args.kwargs["timeout"] == 2.0
        assert out == "停止播放"

    with mock.patch("requests.post", return_value=resp) as m:
        engine.transcribe(audio, 16000)
        assert m.call_args.kwargs["timeout"] == 30.0  # 默认 self.timeout


# ---------- 改动 3：TTS 403 重试 ----------

class Fake403(Exception):
    def __str__(self):
        return "403, message='Invalid response status'"


def _make_tts(tmp_path):
    from app.speech.tts import TTSController
    with mock.patch("app.speech.tts.get_event_bus"):
        return TTSController({
            "engine": "edge",
            "edge_voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "volume": "+0%",
            "output_dir": str(tmp_path),
            "mpv_path": "C:/nope/mpv.exe",
        })


def test_tts_403_retries_then_success(tmp_path):
    """前两次 403 → 第三次成功：返回有效文件，重试 3 次"""
    tts = _make_tts(tmp_path)

    async def save_side(path):
        if not hasattr(save_side, "n"):
            save_side.n = 0
        save_side.n += 1
        if save_side.n < 3:
            raise Fake403()
        with open(path, "wb") as f:
            f.write(b"x" * 2000)          # >1000 字节 → 文件校验通过

    communicate = MagicMock()
    communicate.save = AsyncMock(side_effect=save_side)
    with mock.patch("edge_tts.Communicate", return_value=communicate), \
         mock.patch("time.sleep"):
        out = tts._generate_audio("测试播报")

    assert out is not None and os.path.exists(out)
    assert communicate.save.await_count == 3
    os.remove(out)


def test_tts_403_gives_up_and_cleans(tmp_path):
    """一直 403 → 3 次后放弃返回 None，残留文件被清理"""
    tts = _make_tts(tmp_path)

    async def save_side(path):
        raise Fake403()

    communicate = MagicMock()
    communicate.save = AsyncMock(side_effect=save_side)
    with mock.patch("edge_tts.Communicate", return_value=communicate), \
         mock.patch("time.sleep"):
        out = tts._generate_audio("测试播报")

    assert out is None
    # 残留文件被清理：output_dir 无 tts_ 开头文件
    leftovers = [f for f in os.listdir(str(tmp_path)) if f.startswith("tts_")]
    assert leftovers == []
