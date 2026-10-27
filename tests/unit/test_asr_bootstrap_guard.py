"""asr_bootstrap 空模型防护 + asr.py 400 提示 定向测试（R2/R3 API 版）。"""
import pytest

pytestmark = pytest.mark.unit

import pytest
from app.voice import asr_bootstrap as ab


@pytest.fixture(autouse=True)
def _isolate_ab_state(monkeypatch):
    """每个用例前后重置 asr_bootstrap 的全局进程句柄，并免掉真实 sleep。"""
    monkeypatch.setattr(ab, "_server_proc", None)
    monkeypatch.setattr(ab.time, "sleep", lambda *_a, **_k: None)
    yield
    # 用例结束后清掉可能残留的 FakePopen 引用
    ab._server_proc = None


class TestBootstrapGuard:
    """空模型时绝不 -m "" 启动 llama-server（曾导致 router 空载 400）。"""

    def test_empty_model_returns_none(self, monkeypatch):
        from app.voice import asr_bootstrap as ab

        monkeypatch.setattr(ab, "resolve_model_paths", lambda cfg: ("", ""))
        monkeypatch.setattr(ab, "find_llama_backend_candidates",
                            lambda cfg: [{"backend": "cpu", "exe": "server.exe"}])
        monkeypatch.setattr(ab, "_is_server_up", lambda: False)
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab, "subprocess", None)  # 若误走到 Popen 会 AttributeError

        result = ab.ensure_and_launch({"bootstrap_enabled": True})
        assert result is None  # 模型缺失 → 不启动

    def test_with_model_passes_real_path(self, monkeypatch):
        from types import SimpleNamespace
        from app.voice import asr_bootstrap as ab

        model = r"C:\fake\models\Qwen3-ASR-0.6B-Q8_0.gguf"
        mmproj = r"C:\fake\models\mmproj-Qwen3-ASR-0.6B-bf16.gguf"

        captured = {}

        class FakePopen:
            def __init__(self, args, **kw):
                captured["args"] = args

            def poll(self):
                return None

        fake_subprocess = SimpleNamespace(Popen=FakePopen, DEVNULL=-3, CREATE_NO_WINDOW=0)
        fake_response = SimpleNamespace(status_code=200,
                                        json=lambda: {"data": [{"id": "m"}]})
        monkeypatch.setattr(ab, "resolve_model_paths", lambda cfg: (model, mmproj))
        monkeypatch.setattr(ab, "find_llama_backend_candidates",
                            lambda cfg: [{"backend": "cpu", "exe": "server.exe"}])
        monkeypatch.setattr(ab, "_is_server_up", lambda: False)
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab.os.path, "isfile", lambda p: True)
        monkeypatch.setattr(ab, "subprocess", fake_subprocess)
        monkeypatch.setattr(ab, "_server_proc", None)
        monkeypatch.setattr(ab.requests, "get", lambda *a, **k: fake_response)

        proc = ab.ensure_and_launch({"bootstrap_enabled": True})
        assert proc is not None
        assert captured["args"][1] == "-m"
        assert captured["args"][2] == model  # 真实路径，绝不传空
        assert "--parallel" in captured["args"]

    def test_model_missing_logs_hint(self, monkeypatch, capsys):
        from app.voice import asr_bootstrap as ab

        monkeypatch.setattr(ab, "resolve_model_paths", lambda cfg: ("", ""))
        monkeypatch.setattr(ab, "find_llama_backend_candidates",
                            lambda cfg: [{"backend": "cpu", "exe": "server.exe"}])
        monkeypatch.setattr(ab, "_is_server_up", lambda: False)
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab, "subprocess", None)

        ab.ensure_and_launch({"bootstrap_enabled": True})
        out = capsys.readouterr().out
        assert "模型缺失" in out


class TestAsr400Hint:
    """转录 HTTP 400 时给出"未加载模型"提示。"""

    def test_400_hint(self, monkeypatch, capsys):
        import requests as requests_mod
        from app.voice.asr import Qwen3ServerASR

        asr = Qwen3ServerASR.__new__(Qwen3ServerASR)
        asr._loaded = True
        asr.model = "default"
        asr.base_url = "http://127.0.0.1:8080/v1"
        asr.timeout = 30.0
        asr.server_url = "http://127.0.0.1:8080/v1"

        class R400:
            status_code = 400

            def json(self):
                return {}

        monkeypatch.setattr(requests_mod, "post", lambda *a, **k: R400())
        text = asr.transcribe(b"audio")
        out = capsys.readouterr().out
        assert text == ""
        assert "未加载任何模型" in out

    def test_500_no_hint(self, monkeypatch, capsys):
        import requests as requests_mod
        from app.voice.asr import Qwen3ServerASR

        asr = Qwen3ServerASR.__new__(Qwen3ServerASR)
        asr._loaded = True
        asr.model = "default"
        asr.base_url = "http://127.0.0.1:8080/v1"
        asr.timeout = 30.0
        asr.server_url = "http://127.0.0.1:8080/v1"

        class R500:
            status_code = 500

            def json(self):
                return {}

        monkeypatch.setattr(requests_mod, "post", lambda *a, **k: R500())
        asr.transcribe(b"audio")
        out = capsys.readouterr().out
        assert "未加载任何模型" not in out
