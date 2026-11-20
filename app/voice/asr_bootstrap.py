"""ASR 资源自举（模型就绪 + llama-server 自动拉起 + 双后端回退）。

R2/R3（2026-10-26）：
- Vulkan 后端优先，CPU 后端回退（find_llama_backend_candidates）。
- 模型目录 = LOCALAPPDATA\\Starlight\\models（纯 ASCII，避免中文路径打不开 GGUF）。
- 种子迁移：安装版 Program Files/models → 用户目录（首启拷贝，幂等，不删原种）。
- 启动参数 -c 8192 --parallel 2 --port 8080。
- 8080 被外部占用（非本应用 ASR）→ 不拉起、不杀外部进程、降级。
"""
import atexit
import http.client
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests

from app.core.path_resolve import (
    PROJECT_ROOT,
    find_llama_backend_candidates,
)
from .asr_health import model_list_ready, read_model_list

# 用户可写模型目录：固定用 %LOCALAPPDATA%\Starlight\models（纯 ASCII 路径）。
# 原因：llama-server 在 Windows 下打开含中文/非 ASCII 的模型路径会失败
# （stderr: failed to open GGUF file，路径乱码）。源码态项目目录可能含中文
# （如 C:\娱乐助手\Starlight），不能作为模型加载路径。
def _resolve_user_model_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Starlight" / "models"


USER_MODEL_DIR = _resolve_user_model_dir()
# 安装包种子模型目录（frozen 态 = Program Files\星芒\models；源码态 = 项目 models/）
SEED_MODEL_DIR = PROJECT_ROOT / "models"

PORT = 8080

# 国内直连镜像（hf-mirror.com = HuggingFace 官方镜像站，直连免代理）
_MODEL_REPO = "ggml-org/Qwen3-ASR-0.6B-GGUF"
_MODEL_URLS: dict[str, str] = {
    "Qwen3-ASR-0.6B-Q8_0.gguf":
        f"https://hf-mirror.com/{_MODEL_REPO}/resolve/main/Qwen3-ASR-0.6B-Q8_0.gguf",
    "mmproj-Qwen3-ASR-0.6B-bf16.gguf":
        f"https://hf-mirror.com/{_MODEL_REPO}/resolve/main/mmproj-Qwen3-ASR-0.6B-bf16.gguf",
}

# 模块级：被自举拉起的 llama-server 进程（atexit 清理，防僵尸）
_server_proc: subprocess.Popen | None = None
_server_backend: str = ""  # 记录当前拉起的后端类型（vulkan/cpu/...）
_atexit_registered = False
_lock = threading.Lock()
_startup_cancel: threading.Event | None = None


def _is_server_up(timeout: float = 0.8) -> bool:
    """Check for a loaded model on the local endpoint, without touching model files."""
    try:
        status, payload = read_model_list(f"http://127.0.0.1:{PORT}/v1", timeout)
        return status == 200 and model_list_ready(payload)
    except (OSError, ValueError, http.client.HTTPException, requests.RequestException):
        return False


def _port_in_use() -> bool:
    """Check ownership even on Windows where an unused loopback port may time out."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", PORT))
        return False
    except OSError:
        return True  # Occupied or unsafe to claim.


def _seed_models_if_needed(cancel: threading.Event | None = None) -> None:
    """R3: 种子模型迁移——安装版 Program Files/models → 用户目录。

    规则：
    - 用户目录已有完整模型 → 不拷贝（幂等）。
    - 用户目录缺失但种子目录有 → 拷贝（临时文件 + os.replace 原子切换）。
    - 拷贝失败不删除种子、不标记成功、不抛异常。
    - 源码态 USER_MODEL_DIR == SEED_MODEL_DIR，直接跳过。
    """
    try:
        if USER_MODEL_DIR.resolve() == SEED_MODEL_DIR.resolve():
            return  # 开发态同目录，无需迁移
        for name in ("Qwen3-ASR-0.6B-Q8_0.gguf", "mmproj-Qwen3-ASR-0.6B-bf16.gguf"):
            if cancel is not None and cancel.is_set():
                return
            src = SEED_MODEL_DIR / name
            dst = USER_MODEL_DIR / name
            if dst.is_file() and dst.stat().st_size > 0:
                continue  # 用户已有，不覆盖
            if not src.is_file():
                continue  # 种子也没有（轻量版场景），交给下载
            USER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
            fd, name_in_progress = tempfile.mkstemp(suffix=".tmp", dir=dst.parent)
            tmp = Path(name_in_progress)
            try:
                with os.fdopen(fd, "wb") as output, src.open("rb") as input_file:
                    while cancel is None or not cancel.is_set():
                        chunk = input_file.read(1 << 20)
                        if not chunk:
                            break
                        output.write(chunk)
                if cancel is not None and cancel.is_set():
                    return
                if tmp.stat().st_size == src.stat().st_size:
                    shutil.copystat(src, tmp)
                    if cancel is None or not cancel.is_set():
                        os.replace(tmp, dst)
                        print(f"[ASR] 种子模型迁移完成: {name} ({dst.stat().st_size // (1 << 20)}MB)")
                else:
                    print(f"[ASR] 种子模型迁移大小校验失败: {name}")
            finally:
                tmp.unlink(missing_ok=True)
    except Exception as e:
        print(f"[ASR] 种子迁移异常（不阻塞启动）: {e}")


def resolve_model_paths(asr_config: dict) -> tuple[str, str]:
    """模型路径解析：配置显式路径(isfile) → 用户目录 → 种子目录 → 空。"""
    cfg_model = str((asr_config or {}).get("qwen3_asr_model") or "").strip()
    cfg_mmproj = str((asr_config or {}).get("qwen3_asr_mmproj") or "").strip()
    if cfg_model and os.path.isfile(cfg_model):
        mm = cfg_mmproj if (cfg_mmproj and os.path.isfile(cfg_mmproj)) else ""
        return cfg_model, mm
    # 用户目录（种子迁移后落这里）
    user_model = USER_MODEL_DIR / "Qwen3-ASR-0.6B-Q8_0.gguf"
    user_mm = USER_MODEL_DIR / "mmproj-Qwen3-ASR-0.6B-bf16.gguf"
    if user_model.is_file():
        return str(user_model), str(user_mm if user_mm.is_file() else "")
    # 种子目录（Program Files 下安装包内置，只读可读）
    seed_model = SEED_MODEL_DIR / "Qwen3-ASR-0.6B-Q8_0.gguf"
    seed_mm = SEED_MODEL_DIR / "mmproj-Qwen3-ASR-0.6B-bf16.gguf"
    if seed_model.is_file():
        return str(seed_model), str(seed_mm if seed_mm.is_file() else "")
    return "", ""


def download_file(url: str, dest: Path, *, cancel: threading.Event | None = None) -> bool:
    """Cancel between reads; the current connect/read remains governed by requests."""
    tmp = None
    try:
        if cancel is not None and cancel.is_set():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=(10, 60)) as r:
            if cancel is not None and cancel.is_set():
                return False
            if r.status_code != 200:
                print(f"[ASR] 下载失败 HTTP {r.status_code}: {url}")
                return False
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            fd, name_in_progress = tempfile.mkstemp(suffix=".part", dir=dest.parent)
            tmp = Path(name_in_progress)
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if cancel is not None and cancel.is_set():
                        return False
                    if chunk:
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            print(f"\r[ASR] 下载 {dest.name}: {got * 100.0 / total:5.1f}%",
                                  end="", flush=True)
            print()
        if cancel is not None and cancel.is_set():
            return False
        os.replace(tmp, dest)
        return True
    except Exception as e:
        print(f"[ASR] 下载异常 {url}: {e}")
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as e:
                print(f"[ASR] 临时下载文件清理失败: {e}")


def ensure_assets(asr_config: dict, *, cancel: threading.Event | None = None) -> bool:
    """Prepare model files cooperatively without replacing an incomplete model."""
    if cancel is not None and cancel.is_set():
        return False
    if cancel is None:
        _seed_models_if_needed()
    else:
        _seed_models_if_needed(cancel)
    if cancel is not None and cancel.is_set():
        return False
    model, _ = resolve_model_paths(asr_config)
    if model and os.path.isfile(model):
        return True
    USER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, url in _MODEL_URLS.items():
        if cancel is not None and cancel.is_set():
            return False
        dest = USER_MODEL_DIR / name
        if dest.is_file():
            continue
        print(f"[ASR] 未找到 {name}，从国内镜像自动下载（hf-mirror.com，约数百 MB，请耐心等待）…")
        downloaded = download_file(url, dest) if cancel is None else download_file(url, dest, cancel=cancel)
        if not downloaded:
            ok = False
    return ok


def _wait(delay: float, cancel: threading.Event | None) -> bool:
    if cancel is None:
        time.sleep(delay)
        return False
    return cancel.wait(delay)


def _try_start_backend(candidate: dict, model: str, mmproj: str, ctx: int,
                       ngl: int = 0, *, cancel: threading.Event | None = None) -> tuple[subprocess.Popen | None, str]:
    """尝试用指定后端启动 llama-server。

    返回 (proc, stderr_tail)：成功时 stderr_tail 为空；失败时为最后若干行启动日志，
    供上层打印真实错误（不再把 stderr 丢 DEVNULL 吞掉，避免用户只看到 code=1）。
    """
    exe = candidate["exe"]
    backend = candidate["backend"]
    args = [exe, "-m", model, "--port", str(PORT), "--host", "127.0.0.1",
            "-c", str(ctx), "--parallel", "2"]
    if ngl and ngl > 0:
        args += ["-ngl", str(ngl)]
    if mmproj and os.path.isfile(mmproj):
        args += ["--mmproj", mmproj]
    ngl_note = f", -ngl={ngl} (GPU offload)" if ngl and ngl > 0 else ", CPU only"
    print(f"[ASR] 尝试后端: {backend} ({exe}){ngl_note}")
    # stderr 落临时文件，失败时读尾部（不再 DEVNULL 吞错误）
    err_path = os.path.join(tempfile.gettempdir(), "starlight_llama_stderr.log")
    global _server_proc, _server_backend, _atexit_registered
    try:
        with open(err_path, "wb") as err_fp:
            # Admission and PID registration are one critical section. A concurrent
            # shutdown either prevents admission or obtains this exact process.
            with _lock:
                if cancel is not None and cancel.is_set():
                    return None, ""
                proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=err_fp,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                _server_proc = proc
                _server_backend = backend
                if not _atexit_registered:
                    atexit.register(_kill_server)
                    _atexit_registered = True
    except Exception as e:
        print(f"[ASR] 后端 {backend} 启动失败: {e}")
        return None, str(e)

    ready = False
    try:
        if _wait(2, cancel):
            return None, ""
        # Release the ownership lock before waiting on any backend or HTTP probe.
        for _ in range(6):
            if proc.poll() is not None or _wait(2, cancel):
                break
            if proc.poll() is not None:
                break
            if _is_server_up(timeout=3):
                with _lock:
                    ready = _server_proc is proc and (cancel is None or not cancel.is_set())
                if ready:
                    print(f"[ASR] 后端 {backend} 模型已加载就绪")
                    return proc, ""
                return None, ""
        if cancel is not None and cancel.is_set():
            return None, ""
        tail = _read_stderr_tail(err_path, n=30)
        print(f"[ASR] 后端 {backend} 未就绪 (code={proc.poll()})，最后日志:")
        for line in tail.splitlines():
            print(f"      | {line}")
        return None, tail
    finally:
        if not ready:
            _stop_owned(proc)


def _read_stderr_tail(path: str, n: int = 30) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def ensure_and_launch(asr_config: dict, *, cancel: threading.Event | None = None) -> subprocess.Popen | None:
    """Reuse an existing listener, or start a single owned backend."""
    global _startup_cancel
    if not (asr_config or {}).get("bootstrap_enabled", True):
        return None
    cancel = cancel if cancel is not None else threading.Event()
    with _lock:
        if _startup_cancel is not None or cancel.is_set():
            return None
        _startup_cancel = cancel
    try:
        return _ensure_and_launch(asr_config, cancel)
    finally:
        with _lock:
            _startup_cancel = None


def _ensure_and_launch(asr_config: dict, cancel: threading.Event) -> subprocess.Popen | None:
    if _is_server_up():
        return None  # 外部已有可用 ASR，直接复用
    if _port_in_use():
        for _ in range(3):
            if _wait(0.25, cancel) or _is_server_up(timeout=0.4):
                return None
            if not _port_in_use():
                break
        else:
            print("[ASR] 本地端口有监听服务，但模型尚未就绪，暂不启动新进程。")
            return None
    with _lock:
        if _server_proc is not None and _server_proc.poll() is None:
            return _server_proc
    if cancel.is_set() or not ensure_assets(asr_config, cancel=cancel) or cancel.is_set():
        return None
    model, mmproj = resolve_model_paths(asr_config)
    if not model or not os.path.isfile(model):
        print("[ASR] 模型缺失，跳过启动 llama-server")
        return None
    ctx = int((asr_config or {}).get("llama_ctx") or 8192)
    ngl = int((asr_config or {}).get("llama_gpu_layers") or 0)
    candidates = find_llama_backend_candidates(asr_config)
    if not candidates:
        print("[ASR] 未找到任何 llama-server 后端（打包内置/PATH/LM Studio 均无）")
        return None
    print(f"[ASR] 检测到 {len(candidates)} 个后端候选: "
          + ", ".join(f"{c['backend']}({c['exe']})" for c in candidates))
    want = str((asr_config or {}).get("llama_backend") or "auto").lower()
    print(f"[ASR] llama_backend 偏好={want}，GPU offload layers={ngl}"
          + ("（将传 -ngl）" if ngl > 0 else "（CPU 推理）"))
    # 主用模型路径；若主路径含非 ASCII 字符（llama.cpp 在 Windows 下打开非 ASCII 路径会失败），
    # 自动准备一个 ASCII 用户目录副本作为回退路径。
    fallback_model, fallback_mm = _ascii_fallback_paths(model, mmproj)
    if cancel.is_set() or _port_in_use():
        return None  # An external listener can appear while preparing the model.
    tried = set()
    for cand in candidates:
        for path, projection in ((model, mmproj), (fallback_model, fallback_mm)):
            if cancel.is_set():
                return None
            key = (cand["exe"], path)
            if not path or key in tried:
                continue
            tried.add(key)
            if path != model:
                print(f"[ASR] 主模型路径启动失败，尝试 ASCII 副本: {path}")
            proc, _ = _try_start_backend(cand, path, projection, ctx, ngl=ngl, cancel=cancel)
            if proc is not None:
                return proc
    print("[ASR] 所有后端均启动失败，语音功能降级（不影响音乐/电影/LoL）")
    return None


def _ascii_fallback_paths(model: str, mmproj: str) -> tuple[str, str]:
    """若主模型路径含非 ASCII 字符，返回用户目录下的 ASCII 副本路径（若存在）。

    llama.cpp on Windows 在非 UTF-8 系统区域设置下打开含中文/日文的模型路径会失败
    （stderr: failed to open GGUF file，路径乱码）。用户目录 LOCALAPPDATA\\Starlight\\models
    是纯 ASCII 路径，作为回退。
    """
    try:
        model_is_ascii = model.isascii() if model else True
        if model_is_ascii:
            return "", ""
        cand_model = USER_MODEL_DIR / "Qwen3-ASR-0.6B-Q8_0.gguf"
        cand_mm = USER_MODEL_DIR / "mmproj-Qwen3-ASR-0.6B-bf16.gguf"
        if cand_model.is_file():
            return str(cand_model), str(cand_mm if cand_mm.is_file() else "")
    except Exception:
        pass
    return "", ""


def _stop_owned(expected=None, *, cancel_startup: bool = False) -> None:
    """Take ownership once, then reap without blocking the admission lock."""
    global _server_proc, _server_backend
    with _lock:
        if cancel_startup and _startup_cancel is not None:
            _startup_cancel.set()
        proc = _server_proc
        if expected is not None and expected is not proc:
            return
        _server_proc = None
        _server_backend = ""
    reap_backend(proc)


def reap_backend(proc) -> None:
    """Only operate on a specific Popen owned by this application."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception as e:
            print(f"[ASR] 未确认子进程退出: {e}")


def _kill_server() -> None:
    # Signal immediately even if Windows is still returning an admitted Popen handle.
    cancel = _startup_cancel
    if cancel is not None:
        cancel.set()
    _stop_owned(cancel_startup=True)


def shutdown_server() -> None:
    """Cancel model preparation and reclaim the owned backend, if there is one."""
    _kill_server()
