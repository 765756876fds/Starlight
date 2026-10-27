"""ASR 自举（R2/R3）测试：模型路径解析 / hf-mirror 下载 / llama-server 双后端拉起 / 退出清理。

对齐 asr_bootstrap R2/R3 API（USER_MODEL_DIR / SEED_MODEL_DIR / resolve_model_paths /
find_llama_backend_candidates / _try_start_backend）——旧测试引用的 MODEL_DIR /
resolve_llama_server 已被 R2/R3 重写移除，属历史遗留，本文件随 API 一并更新。
"""
import pytest

pytestmark = pytest.mark.unit

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
    def _isolate_dirs(self, tmp_path, monkeypatch):
        """把用户/种子模型目录隔离到临时目录（避免命中本机真实模型）。"""
        monkeypatch.setattr(ab, "USER_MODEL_DIR", tmp_path / "user")
        monkeypatch.setattr(ab, "SEED_MODEL_DIR", tmp_path / "seed")

    def test_config_path_wins(self, tmp_path, monkeypatch):
        model = _touch(tmp_path / "m.gguf")
        mm = _touch(tmp_path / "mmproj.gguf")
        self._isolate_dirs(tmp_path, monkeypatch)
        got, got_mm = ab.resolve_model_paths(
            {"qwen3_asr_model": model, "qwen3_asr_mmproj": mm})
        assert got == model and got_mm == mm

    def test_user_dir(self, tmp_path, monkeypatch):
        """用户目录（种子迁移后落这里）有完整模型 → 命中。"""
        user = tmp_path / "user"
        user.mkdir()
        _touch(user / "Qwen3-ASR-0.6B-Q8_0.gguf")
        _touch(user / "mmproj-Qwen3-ASR-0.6B-bf16.gguf")
        self._isolate_dirs(tmp_path, monkeypatch)
        model, mm = ab.resolve_model_paths({})
        assert model == str(user / "Qwen3-ASR-0.6B-Q8_0.gguf")
        assert mm == str(user / "mmproj-Qwen3-ASR-0.6B-bf16.gguf")

    def test_seed_dir_fallback(self, tmp_path, monkeypatch):
        """用户目录无模型 → 回退种子目录（安装包内置，只读可读）。"""
        seed = tmp_path / "seed"
        seed.mkdir()
        _touch(seed / "Qwen3-ASR-0.6B-Q8_0.gguf")
        _touch(seed / "mmproj-Qwen3-ASR-0.6B-bf16.gguf")
        self._isolate_dirs(tmp_path, monkeypatch)
        model, mm = ab.resolve_model_paths({})
        assert model == str(seed / "Qwen3-ASR-0.6B-Q8_0.gguf")
        assert mm == str(seed / "mmproj-Qwen3-ASR-0.6B-bf16.gguf")

    def test_missing_returns_empty(self, tmp_path, monkeypatch):
        self._isolate_dirs(tmp_path, monkeypatch)
        assert ab.resolve_model_paths({}) == ("", "")

    def test_config_broken_path_falls_to_user_dir(self, tmp_path, monkeypatch):
        user = tmp_path / "user"
        user.mkdir()
        _touch(user / "Qwen3-ASR-0.6B-Q8_0.gguf")
        self._isolate_dirs(tmp_path, monkeypatch)
        model, _ = ab.resolve_model_paths({"qwen3_asr_model": "D:/nope/x.gguf"})
        assert model == str(user / "Qwen3-ASR-0.6B-Q8_0.gguf")


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
        user = tmp_path / "user"
        user.mkdir()
        _touch(user / "Qwen3-ASR-0.6B-Q8_0.gguf")
        _touch(user / "mmproj-Qwen3-ASR-0.6B-bf16.gguf")
        monkeypatch.setattr(ab, "USER_MODEL_DIR", user)
        monkeypatch.setattr(ab, "SEED_MODEL_DIR", tmp_path / "seed")
        monkeypatch.setattr(ab.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载")))
        assert ab.ensure_assets({}) is True

    def test_missing_downloads_all(self, tmp_path, monkeypatch):
        user = tmp_path / "user"
        monkeypatch.setattr(ab, "USER_MODEL_DIR", user)
        monkeypatch.setattr(ab, "SEED_MODEL_DIR", tmp_path / "seed")
        monkeypatch.setattr(ab, "download_file", lambda url, dest: _touch(dest) or True)
        assert ab.ensure_assets({}) is True
        assert (user / "Qwen3-ASR-0.6B-Q8_0.gguf").exists()
        assert (user / "mmproj-Qwen3-ASR-0.6B-bf16.gguf").exists()


class TestEnsureAndLaunch:
    def test_disabled_by_switch(self, monkeypatch):
        assert ab.ensure_and_launch({"bootstrap_enabled": False}) is None

    def test_reuse_external_server(self, monkeypatch):
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab, "_is_server_up", lambda *a, **k: True)
        monkeypatch.setattr(ab, "find_llama_backend_candidates",
                            lambda cfg: [{"backend": "cpu", "exe": "C:/x/llama-server.exe"}])
        monkeypatch.setattr(ab, "subprocess",
                            type("S", (), {"Popen": lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应拉起"))}))
        assert ab.ensure_and_launch({}) is None

    def test_launches_first_candidate(self, tmp_path, monkeypatch):
        """按候选优先级取第一个后端；成功即返回且写入 _server_proc。"""
        model = _touch(tmp_path / "Qwen3-ASR-0.6B-Q8_0.gguf")
        mm = _touch(tmp_path / "mmproj.gguf")
        server = _touch(tmp_path / "llama-server.exe")
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab, "_is_server_up", lambda *a, **k: False)
        monkeypatch.setattr(ab, "resolve_model_paths", lambda cfg: (model, mm))
        monkeypatch.setattr(ab, "find_llama_backend_candidates",
                            lambda cfg: [{"backend": "cpu", "exe": server},
                                         {"backend": "vulkan", "exe": "D:/nope/vulkan.exe"}])
        started = []

        class FakeProc:
            def poll(self):
                return None

        monkeypatch.setattr(ab, "_try_start_backend",
                            lambda cand, m, mmproj, ctx, **kw: (started.append(cand) or FakeProc(), ""))
        proc = ab.ensure_and_launch({})
        assert proc is not None
        assert started and started[0]["backend"] == "cpu"  # 按优先级取第一个候选
        assert ab._server_proc is proc
        ab._server_proc = None  # 清理，防 atexit 误杀测试进程

    def test_backend_args_and_healthcheck(self, tmp_path, monkeypatch):
        """_try_start_backend：参数含 -m/--mmproj/--port/-c/--parallel；健康检查 data 非空才就绪。"""
        model = _touch(tmp_path / "m.gguf")
        mm = _touch(tmp_path / "mmproj.gguf")
        server = _touch(tmp_path / "llama-server.exe")
        calls = {}

        class FakeProc:
            def poll(self):
                return None

            def terminate(self):
                calls["terminated"] = True

            def wait(self, timeout=None):
                calls["waited"] = True

        def _fake_popen(args, **k):
            calls["args"] = args
            return FakeProc()

        monkeypatch.setattr(ab.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(ab.time, "sleep", lambda *a, **k: None)

        class R:
            status_code = 200

            def json(self):
                return {"data": [{"id": "m"}]}

        monkeypatch.setattr(ab.requests, "get", lambda *a, **k: R())
        proc, _ = ab._try_start_backend({"backend": "cpu", "exe": server}, model, mm, 8192)
        assert proc is not None
        args = calls["args"]
        assert args[0] == server
        assert "-m" in args and model in args
        assert "--mmproj" in args and mm in args
        assert "--port" in args and "8080" in args
        assert "-c" in args and "8192" in args
        # --parallel 2：允许 2 个并发请求（主 ASR 与打断旁路并行转写）
        assert "--parallel" in args and "2" in args

    def test_backend_immediate_exit_returns_none(self, tmp_path, monkeypatch):
        """启动后立即退出（端口占用/参数错误）→ 放弃该后端（不抛）。"""
        server = _touch(tmp_path / "llama-server.exe")

        class DeadProc:
            def poll(self):
                return 1

            returncode = 1

        monkeypatch.setattr(ab.subprocess, "Popen", lambda args, **k: DeadProc())
        monkeypatch.setattr(ab.time, "sleep", lambda *a, **k: None)
        proc, tail = ab._try_start_backend({"backend": "cpu", "exe": server}, "m.gguf", "", 8192)
        assert proc is None

    def test_no_candidates_returns_none(self, monkeypatch):
        monkeypatch.setattr(ab, "ensure_assets", lambda cfg: True)
        monkeypatch.setattr(ab, "_is_server_up", lambda *a, **k: False)
        monkeypatch.setattr(ab, "resolve_model_paths", lambda cfg: ("C:/m.gguf", ""))
        monkeypatch.setattr(ab, "find_llama_backend_candidates", lambda cfg: [])
        assert ab.ensure_and_launch({}) is None

    def test_kill_server(self, monkeypatch):
        killed = {}

        class FakeProc:
            def poll(self):
                return None

            def terminate(self):
                killed["t"] = True

            def wait(self, timeout=None):
                killed["w"] = True

        class FakeLock:
            def __enter__(self):
                return None

            def __exit__(self, *a):
                return False

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
            def __init__(self, *a, **k):
                self._ok = False

            def load(self, retry_seconds=0.0):
                self._ok = True

            def is_available(self):
                return self._ok

            def unload(self):
                self._ok = False

        monkeypatch.setattr("app.voice.asr.Qwen3ServerASR", FakeEngine)
        mgr = ASRManager({"engine": "qwen3_server",
                          "bootstrap_enabled": True,
                          "qwen3_server": {"base_url": "http://127.0.0.1:8080/v1", "timeout": 1.0}})
        mgr.initialize()
        assert called  # bootstrap 被调用
        assert len(mgr.engines) == 1
        assert mgr.is_available()
