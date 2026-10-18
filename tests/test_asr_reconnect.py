"""
ASR-RECONNECT-01: ASR 服务挂掉/恢复自动重连测试（修2）

场景：
① 服务挂掉（transcribe 请求异常）→ 返回 ""（不抛异常，不卡线程）
② 服务恢复（active 引擎失联后）→ transcribe 触发重连，load 重新探测成功并返回文本
③ 重连失败（服务一直不可用）→ 返回 ""，不阻塞（retry_seconds=0 单次探测）
④ 无引擎配置 → 返回 ""
"""
import requests

from app.voice.asr import ASRManager

CONFIG = {
    "engine": "qwen3_server",
    "bootstrap_enabled": False,  # 测试隔离：不触发模型下载/拉起 llama-server
    "qwen3_server": {
        "base_url": "http://127.0.0.1:8080/v1",
        "timeout": 1.0,
    },
}

_AUDIO = b"\x00" * 3200  # 1600 samples @16k ≈ 0.1s


def _ok_models_response(*a, **k):
    class R:
        status_code = 200
    return R()


def _ok_transcribe_response(*a, **k):
    class R:
        status_code = 200

        def json(self):
            return {"text": "<asr_text>你好</asr_text>"}
    return R()


def _down_models(*a, **k):
    raise requests.ConnectionError("connection refused")


def _down_transcribe(*a, **k):
    raise requests.ConnectionError("connection refused")


def test_transcribe_empty_when_server_down(monkeypatch):
    monkeypatch.setattr(requests, "get", _ok_models_response)
    monkeypatch.setattr(requests, "post", _down_transcribe)
    mgr = ASRManager(CONFIG)
    mgr.initialize()
    # 服务挂掉：转写失败返回空串，不抛异常（调用线程不挂）
    assert mgr.transcribe(_AUDIO) == ""
    assert mgr.transcribe(_AUDIO) == ""


def test_auto_reconnect_after_server_down(monkeypatch):
    monkeypatch.setattr(requests, "get", _ok_models_response)
    monkeypatch.setattr(requests, "post", _down_transcribe)
    mgr = ASRManager(CONFIG)
    mgr.initialize()
    # 模拟 active 引擎失联（_loaded 被重置，is_available()==False）
    mgr._active_engine._loaded = False
    # 服务恢复：post 恢复正常
    monkeypatch.setattr(requests, "post", _ok_transcribe_response)
    # 再次转写：应触发重连（load 探测）→ 成功返回文本
    assert mgr.transcribe(_AUDIO) == "你好"
    assert mgr._active_engine.is_available() is True


def test_reconnect_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(requests, "get", _ok_models_response)
    monkeypatch.setattr(requests, "post", _down_transcribe)
    mgr = ASRManager(CONFIG)
    mgr.initialize()
    # 模拟失联后服务彻底不可用：load 探测失败 → 返回 ""，不阻塞
    mgr._active_engine._loaded = False
    monkeypatch.setattr(requests, "get", _down_models)
    assert mgr.transcribe(_AUDIO) == ""
    assert mgr._active_engine.is_available() is False


def test_no_engine_returns_empty():
    mgr = ASRManager({})
    assert mgr.transcribe(_AUDIO) == ""
