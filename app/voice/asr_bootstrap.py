"""ASR 资源自举（轻量版安装包：模型缺失自动下载 + llama-server 自动拉起）。

场景：轻量版安装包不含 1.2G 的 ASR 模型，首次在新电脑运行——
1. 检查模型：settings 显式路径(isfile) → 打包内置 models/ → 缺失从
   hf-mirror.com（HuggingFace 国内官方镜像，免代理）自动下载，控制台打印进度。
2. 检查 llama-server.exe：settings asr.llama_server → PATH → 打包内置
   third_party/llama/ → LM Studio backends（复用 find_llama_server）。
3. 8080 端口无服务时自动拉起 llama-server（-m 模型 --mmproj 视觉投影 --port 8080），
   主程序退出时 atexit 连带终止子进程，不留僵尸。

开关：asr.bootstrap_enabled（config.py 默认 True；测试/用户可关）。
模型/llama-server 已存在时零开销（只做 isfile/HTTP 探测，不下载不拉起）。
"""
import atexit
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

from app.core.path_resolve import PROJECT_ROOT, find_llama_server

MODEL_DIR = PROJECT_ROOT / "models"
LLAMA_DIR = PROJECT_ROOT / "third_party" / "llama"
LLAMA_SERVER_EXE = LLAMA_DIR / "llama-server.exe"
PORT = 8080
HEALTH_URL = f"http://127.0.0.1:{PORT}/v1/models"

# 国内直连镜像（hf-mirror.com = HuggingFace 官方镜像站，直连免代理）
_MODEL_REPO = "ggml-org/Qwen3-ASR-0.6B-GGUF"
_MODEL_URLS: Dict[str, str] = {
    "Qwen3-ASR-0.6B-Q8_0.gguf":
        f"https://hf-mirror.com/{_MODEL_REPO}/resolve/main/Qwen3-ASR-0.6B-Q8_0.gguf",
    "mmproj-Qwen3-ASR-0.6B-bf16.gguf":
        f"https://hf-mirror.com/{_MODEL_REPO}/resolve/main/mmproj-Qwen3-ASR-0.6B-bf16.gguf",
}

# 模块级：被自举拉起的 llama-server 进程（atexit 清理，防僵尸）
_server_proc: Optional[subprocess.Popen] = None
_atexit_registered = False
_lock = threading.Lock()


def _is_server_up(timeout: float = 0.8) -> bool:
    """8080 是否已有可用 llama-server（外部常驻服务 → 直接复用不重复拉起）。"""
    try:
        r = requests.get(HEALTH_URL, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def resolve_model_paths(asr_config: dict) -> Tuple[str, str]:
    """模型路径解析（不下载时直接可用）：配置显式路径(isfile) → 打包内置 models/ → 空。

    返回 (model_path, mmproj_path)；空串表示缺失，交由 ensure_assets 下载。
    """
    cfg_model = str((asr_config or {}).get("qwen3_asr_model") or "").strip()
    cfg_mmproj = str((asr_config or {}).get("qwen3_asr_mmproj") or "").strip()
    if cfg_model and os.path.isfile(cfg_model):
        mm = cfg_mmproj if (cfg_mmproj and os.path.isfile(cfg_mmproj)) else ""
        return cfg_model, mm
    builtin_model = MODEL_DIR / "Qwen3-ASR-0.6B-Q8_0.gguf"
    builtin_mm = MODEL_DIR / "mmproj-Qwen3-ASR-0.6B-bf16.gguf"
    if builtin_model.is_file():
        return str(builtin_model), str(builtin_mm if builtin_mm.is_file() else "")
    return "", ""


def resolve_llama_server(asr_config: dict) -> str:
    """llama-server.exe 查找链：配置(isfile) → PATH → 打包内置 third_party/llama → LM Studio。"""
    cfg = str((asr_config or {}).get("llama_server") or "").strip()
    if cfg and os.path.isfile(cfg):
        return cfg
    found = shutil.which("llama-server")
    if found:
        return found
    if LLAMA_SERVER_EXE.is_file():
        return str(LLAMA_SERVER_EXE)
    return find_llama_server(asr_config)  # LM Studio backends glob（取最近安装）


def download_file(url: str, dest: Path) -> bool:
    """流式下载 + 控制台进度（%）。任何失败返回 False 不抛异常（故障隔离）。"""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with requests.get(url, stream=True, timeout=(10, 60)) as r:
            if r.status_code != 200:
                print(f"[ASR] 下载失败 HTTP {r.status_code}: {url}")
                return False
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            pct = got * 100.0 / total
                            print(
                                f"\r[ASR] 下载 {dest.name}: {pct:5.1f}% "
                                f"({got // (1 << 20)}MB/{total // (1 << 20)}MB)",
                                end="", flush=True,
                            )
            print()
        os.replace(tmp, dest)
        return True
    except Exception as e:
        print(f"[ASR] 下载异常 {url}: {e}")
        return False


def ensure_assets(asr_config: dict) -> bool:
    """确保模型就绪。返回 True = 可直接启动；False = 缺失且下载失败（不崩，静默降级）。"""
    model, _ = resolve_model_paths(asr_config)
    if model and os.path.isfile(model):
        return True
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, url in _MODEL_URLS.items():
        dest = MODEL_DIR / name
        if dest.is_file():
            continue
        print(f"[ASR] 未找到 {name}，从国内镜像自动下载（hf-mirror.com，约数百 MB，请耐心等待）…")
        if not download_file(url, dest):
            ok = False
    return ok


def ensure_and_launch(asr_config: dict) -> Optional[subprocess.Popen]:
    """自举总入口（ASRManager.initialize 调用）：

    模型缺失→下载；8080 无服务→拉起 llama-server（atexit 清理）；全部就绪→返回进程。
    """
    global _server_proc, _atexit_registered
    if not (asr_config or {}).get("bootstrap_enabled", True):
        return None
    if not ensure_assets(asr_config):
        return None
    if _is_server_up():
        return None  # 外部已有 llama-server，直接复用
    server_bin = resolve_llama_server(asr_config)
    if not server_bin:
        print("[ASR] 未找到 llama-server.exe（请安装 LM Studio，或放置到 third_party/llama/）")
        return None
    model, mmproj = resolve_model_paths(asr_config)
    with _lock:
        if _server_proc is not None and _server_proc.poll() is None:
            return _server_proc  # 防重入：已拉起且存活，直接复用
        args = [server_bin, "-m", model, "--port", str(PORT), "--host", "127.0.0.1"]
        if mmproj and os.path.isfile(mmproj):
            args += ["--mmproj", mmproj]
        # --parallel 2：允许 2 个并发请求——主 ASR 转写长语音时，
        # 打断旁路的无锁转写可在服务端并行处理，不再排队等主请求完成。
        args += ["--parallel", "2"]
        print(f"[ASR] 拉起 llama-server（模型 {Path(model).name}，端口 {PORT}）…")
        try:
            _server_proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[ASR] llama-server 启动失败: {e}")
            return None
        if not _atexit_registered:
            _atexit_registered = True
            atexit.register(_kill_server)
    return _server_proc


def _kill_server() -> None:
    """主程序退出时终止自举拉起的 llama-server，不留僵尸。"""
    global _server_proc
    with _lock:
        proc = _server_proc
        _server_proc = None
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        except Exception:
            pass
