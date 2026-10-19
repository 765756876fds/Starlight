"""ASR 自举（轻量版）测试：模型路径解析 / hf-mirror 下载 / llama-server 拉起 / 退出清理。"""
import os
import subprocess

import pytest

from app.voice import asr_bootstrap as ab


def _touch(p):
    os.makedirs(os.path.dirname(str(p)), exist_ok=True)
    with open(p, "w") as f:
        f.write("x")
    return str(p)


# ---------- resolve_model_paths ----------

class TestResolveModelPaths:
    def test_config_path_wins(self, tmp_path, monkeypatch):
        model = _touch(tmp_path / "m.gguf")
        mm = _touch(tmp_path / "mmproj.gguf")
        monkeypatch.setattr(ab, "MODEL_DIR", tmp_path)
        got, got_mm = ab.resolve_model_paths(
            {"qwen3_asr_model": model, "qwen3_asr_mmproj": mm})
        assert got == model and got_mm == mm

    def test_builtin_models_dir(self, tmp_path, monkeypatch):
        _touch(tmp_path / "Qwen3-ASR-0.6B-Q8_0.gguf")
        _touch(tmp_path / "mmproj-Qwen3-ASR-0.6B-bf16.gguf")
        monkeypatch.setattr(ab, "MODEL_DIR", tmp_path)
        model, mm = ab.resolve_model_paths({})
        assert model == str(tmp_path / "Qwen3-ASR-0.6B-Q8_0.gguf")
        assert mm == str(tmp_path / "mmproj-Qwen3-ASR-0.6B-bf16.gguf")

    def test_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ab, "MODEL_DIR", tmp_path)
        assert ab.resolve_model_paths({}) == ("", "")

    def test_config_broken_path_falls_to_builtin(self, tmp_path, monkeypatch):
        _touch(tmp_path / "Qwen3-ASR-0.6B-Q8_0.gguf")
        monkeypatch.setattr(ab, "MODEL_DIR", tmp_path)
        model, _ = ab.resolve_model_paths({"qwen3_asr_model": "D:/nope/x.gguf"})
        assert model == str(tmp_path / "Qwen3-ASR-0.6B-Q8_0.gguf")


# ---------- resolve_llama_server ----------

class TestResolveLlamaServer:
    def test_config_first(self, tmp_path, monkeypatch):
        exe = _touch(tmp_path / "llama-server.exe")
        monkeypatch.setattr(ab, "LLAMA_SERVER_EXE", tmp_path / "nope.exe")
        monkeypatch.setattr(ab.shutil, "which", lambda *a, **k: None)
        monkeypatch.setattr(ab, "find_llama_server", lambda *a, **k: "")
        assert ab.resolve_llama_server({"llama_server": exe}) == exe

    def test_which(self, monkeypatch):
        monkeypatch.setattr(ab, "LLAMA_SERVER_EXE", os.path.abspath("nope.exe"))
        monkeypatch.setattr(ab.shutil, "which", lambda *a, **k: "C:/tools/llama-server.exe")
        monkeypatch.setattr(ab, "find_llama_server", lambda *a, **k: "")
        assert ab.resolve_llama_server({}) == "C:/tools/llama-server.exe"

    def test_bundled_dir(self, tmp_path, monkeypatch):
        exe = _touch(tmp_path / "llama-server.exe")
        monkeypatch.setattr(ab, "LLAMA_SERVER_EXE", tmp_path / "llama-server.exe")
        monkeypatch.setattr(ab.shutil, "which", lambda *a, **k: None)
        monkeypatch.setattr(ab, "find_llama_server", lambda *a, **k: "")
        assert ab.resolve_llama_server({}) == exe


# ---------- 下载 ----------

class FakeResp:
    status_code = 200
    headers = {"Content-Length": "6"}

    def iter_content(self, chunk_size=1 << 20):
        yield b"abcdef"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestDownload:
    def test_download_writes_file(self, tmp_path, monkeypatch):
        dest = tmp_path / "model.gguf"
        monkeypatch.setattr(ab.requests, "get",
                            lambda *a, **k: FakeResp())
        assert ab.download_file("https://hf-mirror.com/x/y", dest) is True
        assert dest.read_bytes() == b"abcdef"
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    def test_download_http_error(self, tmp_path, monkeypatch):
        class R:
            status_code = 404
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(ab.requests, "get", lambda *a, **k: R())
        assert ab.download_file("u", tmp_path / "m.gguf") is False

    def test_download_exception(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("offline")
        monkeypatch.setattr(ab.requests, "get", boom)
        assert ab.download_file("u", tmp_path / "m.gguf") is False  # 不抛


# ---------- ensure_assets / ensure_and_launch ----------

class TestEnsureAssets:
    def test_present_skips_download(self, tmp_path, monkeypatch):
        _touch(tmp_path / "Qwen3-ASR-0.6B-Q8_0.gguf")
        monkeypatch.setattr(ab, "MODEL_DIR", tmp_path)
        monkeypatch.setattr(ab.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载")))
        assert ab.ensure_assets({}) is True

    def test_missing_downloads_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ab, "MODEL_DIR", tmp_path)
        monkeypatch.setattr(ab, "download_file", lambda url, dest: _touch(dest) or True)
        assert ab.ensure_assets({}) is True
        assert (tmp_path / "Qwen3-ASR-0.6B-Q8_0.gguf").exists()
        assert (tmp_path / "mmproj-Qwen3-ASR-0.6B-bf16.gguf").exists()


class TestEnsureAndLaunch:
    def test_disabled_by_switch(self, monkeypatch):
        assert ab.ensure_and_launch({"bootstrap_enabled": False}) is None

    def test_reuse_external_server(self, monkeypatch):
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab, "_is_server_up", lambda *a, **k: True)
        monkeypatch.setattr(ab, "resolve_llama_server", lambda cfg: "C:/x/llama-server.exe")
        monkeypatch.setattr(ab, "subprocess", type("S", (), {"Popen": lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应拉起"))}))
        assert ab.ensure_and_launch({}) is None

    def test_launches_server(self, tmp_path, monkeypatch):
        model = _touch(tmp_path / "Qwen3-ASR-0.6B-Q8_0.gguf")
        mm = _touch(tmp_path / "mmproj.gguf")
        server = _touch(tmp_path / "llama-server.exe")
        monkeypatch.setattr(ab, "MODEL_DIR", tmp_path)
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab, "_is_server_up", lambda *a, **k: False)
        monkeypatch.setattr(ab, "resolve_llama_server", lambda cfg: server)
        monkeypatch.setattr(ab, "resolve_model_paths", lambda cfg: (model, mm))
        calls = {}
        class FakeProc:
            def poll(self): return None
        monkeypatch.setattr(ab.subprocess, "Popen",
                            lambda args, **k: calls.setdefault("args", args) or FakeProc())
        proc = ab.ensure_and_launch({})
        assert proc is not None
        assert "llama-server.exe" in calls["args"][0]
        assert "--mmproj" in calls["args"] and mm in calls["args"]
        assert "--port" in calls["args"] and "8080" in calls["args"]
        # --parallel 2：允许 2 个并发请求（主 ASR 与打断旁路并行转写）
        assert "--parallel" in calls["args"] and "2" in calls["args"]
        ab._server_proc = None  # 清理，防 atexit 误杀测试进程

    def test_no_server_bin_returns_none(self, monkeypatch):
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab, "_is_server_up", lambda *a, **k: False)
        monkeypatch.setattr(ab, "resolve_llama_server", lambda cfg: "")
        assert ab.ensure_and_launch({}) is None

    def test_kill_server(self, monkeypatch):
        killed = {}
        class FakeProc:
            def poll(self): return None
            def terminate(self): killed["t"] = True
            def wait(self, timeout=None): killed["w"] = True
        class FakeLock:
            def __enter__(self): return None
            def __exit__(self, *a): return False
        ab._server_proc = FakeProc()
        monkeypatch.setattr(ab, "_lock", FakeLock())
        ab._kill_server()
        assert killed.get("t") is True


# ---------- ASRManager 集成（回归：initialize 挂 bootstrap 不影响引擎创建） ----------

class TestASRManagerIntegration:
    def test_initialize_calls_bootstrap_and_creates_engine(self, monkeypatch):
        from app.voice.asr import ASRManager
        called = {}
        monkeypatch.setattr(ab, "ensure_and_launch",
                            lambda cfg: called.setdefault("cfg", cfg) or None)
        class FakeEngine:
            def __init__(self, *a, **k): self._ok = False
            def load(self, retry_seconds=0.0): self._ok = True
            def is_available(self): return self._ok
            def unload(self): self._ok = False
        monkeypatch.setattr("app.voice.asr.Qwen3ServerASR", FakeEngine)
        mgr = ASRManager({"engine": "qwen3_server",
                          "bootstrap_enabled": True,
                          "qwen3_server": {"base_url": "http://127.0.0.1:8080/v1", "timeout": 1.0}})
        mgr.initialize()
        assert called  # bootstrap 被调用
        assert len(mgr.engines) == 1
        assert mgr.is_available()
