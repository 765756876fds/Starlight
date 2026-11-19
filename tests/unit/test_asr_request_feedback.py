from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from unittest.mock import Mock

import pytest

from app.core.notifications import NotificationCenter, operation_scope
from app.voice import asr_bootstrap as bootstrap
from app.voice.asr import ASRManager, ASRStatus, Qwen3ServerASR
from app.voice.asr_health import model_list_ready
from app.voice.runtime import VoiceRuntime


AUDIO = bytes(3200)
READY = {"data": [{"id": "Qwen3-ASR-0.6B"}]}


@pytest.fixture
def owned_asr(monkeypatch):
    completed = threading.Event()
    state = {
        "models": [READY], "calls": [],
        "transcribe": lambda: (200, {"text": "下一首"}),
        "slow_headers": False, "body_gate": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            content = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            gate = state["body_gate"]
            if gate is not None and self.command == "POST":
                gate.wait(0.8)
            self.wfile.write(content)

        def do_GET(self):
            assert self.path == "/v1/models"
            state["calls"].append(("GET", self.path))
            try:
                if state["slow_headers"]:
                    for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                        if completed.wait(0.04):
                            break
                        self.wfile.write(bytes([byte]))
                    return
                choices = state["models"]
                self.reply(200, choices.pop(0) if len(choices) > 1 else choices[0])
            except OSError:
                pass  # The bounded client closed its own probe.

        def do_POST(self):
            assert self.path == "/v1/audio/transcriptions"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            assert b"audio.wav" in body and b"RIFF" in body
            state["calls"].append(("POST", self.path))
            try:
                self.reply(*state["transcribe"]())
            except OSError:
                pass  # Timeout tests deliberately release a late reply.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    owner = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    owner.start()
    monkeypatch.setattr(bootstrap, "PORT", server.server_port)
    monkeypatch.setattr(bootstrap, "_server_proc", None)
    state["url"] = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        completed.set()
        server.shutdown()
        server.server_close()
        owner.join(1)
        assert not owner.is_alive()


@pytest.mark.parametrize("first", [READY, {"data": []}])
def test_reuse_ready_or_late_listener_without_model_preparation(owned_asr, monkeypatch, tmp_path, first):
    owned_asr["models"] = [first, READY]
    assets = Mock()
    launch = Mock()
    monkeypatch.setattr(bootstrap, "ensure_assets", assets)
    monkeypatch.setattr(bootstrap, "_try_start_backend", launch)
    monkeypatch.setattr(bootstrap, "USER_MODEL_DIR", tmp_path / "no-models")
    assert bootstrap.ensure_and_launch({}) is None
    assert len(owned_asr["calls"]) == (1 if first == READY else 2)
    assets.assert_not_called()
    launch.assert_not_called()
    assert not bootstrap.USER_MODEL_DIR.exists()
    assert bootstrap._server_proc is None
    bootstrap.shutdown_server()
    assert bootstrap._is_server_up()


def test_occupied_but_not_ready_never_starts_second_server(owned_asr, monkeypatch):
    owned_asr["models"] = [{"data": [{"id": "Qwen3-ASR-0.6B", "meta": None}]}]
    assets = Mock()
    launch = Mock()
    monkeypatch.setattr(bootstrap, "ensure_assets", assets)
    monkeypatch.setattr(bootstrap, "_try_start_backend", launch)
    assert not bootstrap._is_server_up()
    assert bootstrap.ensure_and_launch({}) is None
    assert len(owned_asr["calls"]) == 5
    assets.assert_not_called()
    launch.assert_not_called()


def test_slow_headers_cannot_extend_the_loopback_health_deadline(owned_asr):
    owned_asr["slow_headers"] = True
    start = time.monotonic()
    assert not bootstrap._is_server_up(timeout=0.17)
    assert time.monotonic() - start < 0.8
    assert owned_asr["calls"] == [("GET", "/v1/models")]


def test_idle_port_without_model_keeps_original_fallback(owned_asr, monkeypatch, tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    monkeypatch.setattr(bootstrap, "PORT", port)
    monkeypatch.setattr(bootstrap, "USER_MODEL_DIR", tmp_path / "models")
    monkeypatch.setattr(bootstrap, "SEED_MODEL_DIR", tmp_path / "no-seed")
    download = Mock(return_value=False)
    launch = Mock()
    monkeypatch.setattr(bootstrap, "download_file", download)
    monkeypatch.setattr(bootstrap, "_try_start_backend", launch)
    assert bootstrap.ensure_and_launch({}) is None
    assert download.call_count == 2
    assert all(call.args[1].parent == bootstrap.USER_MODEL_DIR for call in download.call_args_list)
    launch.assert_not_called()


@pytest.mark.parametrize(("row", "ready"), [
    ({"id": "old-direct"}, True),
    ({"id": "model", "meta": None}, False),
    ({"id": "", "status": {"value": "loaded"}}, False),
    ({"id": "model", "status": {"value": "loading"}}, False),
    ({"id": "model", "status": {"value": "unloaded"}}, False),
    ({"id": "model", "architecture": {"input_modalities": ["text"]}}, False),
    ({"id": "model", "status": {"value": "loaded"},
      "architecture": {"input_modalities": ["audio", "text"]}}, True),
])
def test_model_list_distinguishes_load_status_and_audio_capability(row, ready):
    assert model_list_ready({"data": [row]}) is ready


@pytest.mark.parametrize(("status", "response", "expected", "text"), [
    (200, {"text": "<asr_text>下一首</asr_text>"}, ASRStatus.TEXT, "下一首"),
    (200, {"text": "<asr_text>\n </asr_text>"}, ASRStatus.EMPTY, ""),
    (200, {"text": None}, ASRStatus.ERROR, ""),
    (200, {"error": {"message": "failed"}}, ASRStatus.ERROR, ""),
    (400, {"error": {"message": "bad input"}}, ASRStatus.ERROR, ""),
    (500, {}, ASRStatus.ERROR, ""),
    (503, {}, ASRStatus.UNAVAILABLE, ""),
])
def test_http_receipt_does_not_confuse_error_with_empty(owned_asr, status, response, expected, text):
    engine = Qwen3ServerASR(owned_asr["url"], timeout=0.8)
    engine.load()
    assert engine.is_available()
    owned_asr["transcribe"] = lambda: (status, response)
    receipt = engine.transcribe_result(AUDIO)
    assert (receipt.status, receipt.text) == (expected, text)
    assert engine.transcribe(AUDIO) == text  # Legacy string consumers, including barge-in.
    assert len([op for op, _ in owned_asr["calls"] if op == "POST"]) == 2


def test_http_timeout_is_per_call_and_connect_failure_is_unavailable(owned_asr):
    engine = Qwen3ServerASR(owned_asr["url"], timeout=0.12)
    engine.load()
    release = threading.Event()

    def slow_reply():
        release.wait(0.8)
        return 200, {"text": ""}

    try:
        owned_asr["transcribe"] = slow_reply
        assert engine.transcribe_result(AUDIO).status is ASRStatus.TIMEOUT
    finally:
        release.set()
    owned_asr["transcribe"] = lambda: (200, {"text": "下一首"})
    assert engine.transcribe_result(AUDIO).text == "下一首"
    # On Windows a closed port may produce a connect *timeout*. Use an
    # accepted connection closed before sending headers to verify ConnectionError.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1)
        engine.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}/v1"

        def disconnect():
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(1)
                peer.recv(4096)

        owner = threading.Thread(target=disconnect)
        owner.start()
        try:
            assert engine.transcribe_result(AUDIO, timeout=1).status is ASRStatus.UNAVAILABLE
        finally:
            owner.join(2)
            assert not owner.is_alive()


def test_transcription_body_timeout_is_not_a_connection_failure(owned_asr):
    engine = Qwen3ServerASR(owned_asr["url"], timeout=0.12)
    engine.load()
    release = owned_asr["body_gate"] = threading.Event()
    try:
        assert engine.transcribe_result(AUDIO).status is ASRStatus.TIMEOUT
    finally:
        release.set()


def test_manager_reprobes_an_unready_model_on_the_next_request(owned_asr):
    manager = ASRManager({"qwen3_server": {"base_url": owned_asr["url"]}})
    owned_asr["models"] = [{"data": []}]
    manager.initialize(bootstrap_result="fail")
    assert not manager.is_available()
    assert manager.transcribe_result(AUDIO).status is ASRStatus.UNAVAILABLE
    assert all(method != "POST" for method, _ in owned_asr["calls"])
    owned_asr["models"] = [READY]
    assert manager.transcribe_result(AUDIO).status is ASRStatus.TEXT
    assert sum(method == "POST" for method, _ in owned_asr["calls"]) == 1
    manager.unload_all()


def make_runtime(owned_asr, monkeypatch):
    center = NotificationCenter()
    monkeypatch.setattr("app.voice.runtime.get_notification_center", lambda: center)
    config = {"qwen3_server": {"base_url": owned_asr["url"], "timeout": 1.2}}
    runtime = VoiceRuntime({"asr": config})
    runtime.asr = ASRManager(config)
    runtime.asr.initialize(bootstrap_result="ok")
    runtime._execute_intent = Mock()
    return runtime, center


def test_runtime_multiple_requests_keep_their_own_notifications(owned_asr, monkeypatch):
    runtime, center = make_runtime(owned_asr, monkeypatch)
    responses = [(500, {}), (200, {"text": ""}), (200, {"text": "下一首"})]
    owned_asr["transcribe"] = lambda: responses.pop(0)
    operations = []
    for i in range(3):
        operation = center.post("正在识别", source="voice", phase="recognizing")
        operations.append(operation)
        with operation_scope(operation):
            runtime._process_speech(AUDIO + bytes([i, 0]), 16000)
    assert [center.get(op).title for op in operations] == [
        "语音识别服务出错", "没有听清楚，请再说一次", "下一首"]
    assert "音乐和电影仍可正常使用" in center.get(operations[0]).message
    assert runtime._execute_intent.call_count == 1
    runtime.stop()


@pytest.mark.parametrize("close", ["mute", "disable", "stop"])
def test_late_http_failure_cannot_overwrite_new_feedback(owned_asr, monkeypatch, close):
    runtime, center = make_runtime(owned_asr, monkeypatch)
    runtime._running = True
    entered, release = threading.Event(), threading.Event()

    def late_error():
        entered.set()
        release.wait(1)
        return 500, {}

    def process(operation):
        with operation_scope(operation):
            runtime._process_speech(AUDIO, 16000)

    old = center.post("正在识别", source="voice", phase="recognizing")
    owned_asr["transcribe"] = late_error
    worker = threading.Thread(target=process, args=(old,))
    worker.start()
    try:
        assert entered.wait(1)
        if close == "disable":
            runtime.set_asr_enabled(False)
            runtime.set_asr_enabled(True)
        elif close == "mute":
            runtime._mic_on = True
            assert runtime.toggle_mic() is False
        else:
            runtime.stop()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert center.get(old).title == "本次识别已结束"
    assert "服务出错" not in runtime.get_activity().get("feedback", "")
    runtime._execute_intent.assert_not_called()
    runtime.stop()


def test_barge_in_http_bypasses_main_request_lock(owned_asr, monkeypatch):
    runtime, _ = make_runtime(owned_asr, monkeypatch)
    began, release = threading.Event(), threading.Event()
    sequence = iter([True, False])

    def transcribe():
        if next(sequence):
            began.set()
            release.wait(1)
            return 200, {"text": ""}
        return 200, {"text": "停止播放"}

    owned_asr["transcribe"] = transcribe
    worker = threading.Thread(target=runtime._process_speech, args=(AUDIO, 16000))
    worker.start()
    try:
        assert began.wait(1)
        runtime._stop_tts_now = Mock()
        runtime._detect_interrupt(AUDIO, 16000)
        runtime._stop_tts_now.assert_called_once()
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(2)
        runtime.stop()
    assert not worker.is_alive()
