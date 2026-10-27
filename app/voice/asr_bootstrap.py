"""ASR 资源自举（模型就绪 + llama-server 自动拉起 + 双后端回退）。

R2/R3（2026-10-26）：
- Vulkan 后端优先，CPU 后端回退（find_llama_backend_candidates）。
- 模型目录 = USER_DATA_DIR/models（frozen 态 = %LOCALAPPDATA%\Starlight\models）。
- 种子迁移：安装版 Program Files/models → 用户目录（首启拷贝，幂等，不删原种）。
- 启动参数 -c 8192 --parallel 2 --port 8080。
- 8080 被外部占用（非本应用 ASR）→ 不拉起、不杀外部进程、降级。
"""
import atexit
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests

from app.core.path_resolve import (
    PROJECT_ROOT,
    USER_DATA_DIR,
    find_llama_backend_candidates,
)

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
HEALTH_URL = f"http://127.0.0.1:{PORT}/v1/models"

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


def _is_server_up(timeout: float = 0.8) -> bool:
    """8080 是否已有可用 llama-server（外部常驻服务 → 直接复用不重复拉起）。
    必须 /v1/models 的 data 非空才算就绪——llama-server 在 router 模式（-m ""）
    下 HTTP 200 但 data 为空，若只看 200 会误判复用坏服务。
    """
    try:
        r = requests.get(HEALTH_URL, timeout=timeout)
        if r.status_code != 200:
            return False
        payload = r.json()
        return bool(payload.get("data") or [])
    except Exception:
        return False


def _seed_models_if_needed() -> None:
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
            src = SEED_MODEL_DIR / name
            dst = USER_MODEL_DIR / name
            if dst.is_file() and dst.stat().st_size > 0:
                continue  # 用户已有，不覆盖
            if not src.is_file():
                continue  # 种子也没有（轻量版场景），交给下载
            USER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            shutil.copy2(src, tmp)
            # 大小校验后才原子替换
            if tmp.stat().st_size == src.stat().st_size:
                os.replace(tmp, dst)
                print(f"[ASR] 种子模型迁移完成: {name} ({dst.stat().st_size // (1 << 20)}MB)")
            else:
                try:
                    tmp.unlink()
                except Exception:
                    pass
                print(f"[ASR] 种子模型迁移大小校验失败: {name}，已回滚临时文件")
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
    """确保模型就绪。先触发种子迁移，再查路径；都没有才下载。返回 True = 可直接启动。"""
    _seed_models_if_needed()
    model, _ = resolve_model_paths(asr_config)
    if model and os.path.isfile(model):
        return True
    USER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, url in _MODEL_URLS.items():
        dest = USER_MODEL_DIR / name
        if dest.is_file():
            continue
        print(f"[ASR] 未找到 {name}，从国内镜像自动下载（hf-mirror.com，约数百 MB，请耐心等待）…")
        if not download_file(url, dest):
            ok = False
    return ok


def _try_start_backend(candidate: dict, model: str, mmproj: str, ctx: int,
                       ngl: int = 0) -> tuple[subprocess.Popen | None, str]:
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
    err_fp = open(err_path, "wb")
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=err_fp,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        err_fp.close()
        print(f"[ASR] 后端 {backend} 启动失败: {e}")
        return None, str(e)
    # 等 2 秒看是否立即退出（端口占用/参数错误/模型加载失败）
    time.sleep(2)
    if proc.poll() is not None:
        err_fp.close()
        tail = _read_stderr_tail(err_path, n=30)
        print(f"[ASR] 后端 {backend} 启动后立即退出 (code={proc.returncode})，最后日志:")
        for line in tail.splitlines():
            print(f"      | {line}")
        return None, tail
    # 健康检查：data 非空才算就绪
    for _ in range(6):
        time.sleep(2)
        try:
            r = requests.get(HEALTH_URL, timeout=3)
            if r.status_code == 200 and r.json().get("data"):
                print(f"[ASR] 后端 {backend} 模型已加载就绪")
                err_fp.close()
                return proc, ""
        except Exception:
            continue
    err_fp.close()
    tail = _read_stderr_tail(err_path, n=30)
    print(f"[ASR] 后端 {backend} 启动但模型未在窗口期内就绪，终止该进程。最后日志:")
    for line in tail.splitlines():
        print(f"      | {line}")
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return None, tail


def _read_stderr_tail(path: str, n: int = 30) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def ensure_and_launch(asr_config: dict) -> subprocess.Popen | None:
    """自举总入口：

    1. ensure_assets（种子迁移 + 必要时下载模型）
    2. 8080 已有可用 ASR 服务 → 直接复用
    3. 按 vulkan→cpu 顺序尝试启动后端，首个成功即返回
    4. 全部失败 → 返回 None（语音功能降级，不影响其他）
    """
    global _server_proc, _server_backend, _atexit_registered
    if not (asr_config or {}).get("bootstrap_enabled", True):
        return None
    if not ensure_assets(asr_config):
        return None
    if _is_server_up():
        return None  # 外部已有可用 ASR，直接复用
    with _lock:
        if _server_proc is not None and _server_proc.poll() is None:
            return _server_proc  # 防重入
    model, mmproj = resolve_model_paths(asr_config)
    if not model or not os.path.isfile(model):
        print(f"[ASR] 模型缺失，跳过启动 llama-server")
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
    with _lock:
        tried = set()
        for cand in candidates:
            key = (cand["exe"], model)
            if key in tried:
                continue
            tried.add(key)
            proc, tail = _try_start_backend(cand, model, mmproj, ctx, ngl=ngl)
            if proc is not None:
                _server_proc = proc
                _server_backend = cand["backend"]
                if not _atexit_registered:
                    _atexit_registered = True
                    atexit.register(_kill_server)
                return proc
            # 主路径失败 + 有 ASCII 回退副本 → 用回退路径再试一次本后端
            if fallback_model and fallback_model != model:
                print(f"[ASR] 主模型路径启动失败（可能是非 ASCII 路径），"
                      f"尝试 ASCII 副本: {fallback_model}")
                key2 = (cand["exe"], fallback_model)
                if key2 not in tried:
                    tried.add(key2)
                    proc, tail = _try_start_backend(cand, fallback_model,
                                                    fallback_mm, ctx, ngl=ngl)
                    if proc is not None:
                        _server_proc = proc
                        _server_backend = cand["backend"]
                        if not _atexit_registered:
                            _atexit_registered = True
                            atexit.register(_kill_server)
                        return proc
        print("[ASR] 所有后端均启动失败，语音功能降级（不影响音乐/电影/LoL）")
        return None


def _ascii_fallback_paths(model: str, mmproj: str) -> tuple[str, str]:
    """若主模型路径含非 ASCII 字符，返回用户目录下的 ASCII 副本路径（若存在）。

    llama.cpp on Windows 在非 UTF-8 系统区域设置下打开含中文/日文的模型路径会失败
    （stderr: failed to open GGUF file '...' 路径乱码）。用户目录 %LOCALAPPDATA%\Starlight\models
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
