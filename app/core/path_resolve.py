"""路径解析工具：外部可执行文件查找链（配置 → 自动搜索 → 降级）。
P0 路径自适应（2026-10-16）：llama-server 定位统一走这里；
P1（2026-10-16）：PROJECT_ROOT 单一来源，lol_data / music_sources 路径统一基于它。
P2（2026-10-18）：PROJECT_ROOT 打包态适配（sys.frozen → exe 目录）+
resolve_under_root 统一把相对配置路径绝对化（消除 CWD 依赖）。
"""
import glob
import os
import shutil
import sys
from pathlib import Path


def _compute_project_root() -> Path:
    """源码态：app/core/path_resolve.py → 上溯 2 层；打包态（PyInstaller）：exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


# 项目根。所有 data/、config/、logs/ 路径的单一来源（不依赖 CWD）。
PROJECT_ROOT = _compute_project_root()


def resolve_under_root(path: str) -> str:
    """把配置里的相对路径绝对化（基于 PROJECT_ROOT）；绝对路径原样返回。

    - "./data/music.db" → <PROJECT_ROOT>/data/music.db
    - "data/player.db"  → <PROJECT_ROOT>/data/player.db
    - "D:\\xxx\\a.db"     → 原样（用户绝对路径不动）
    """
    if not path:
        return path
    p = str(path).strip()
    if os.path.isabs(p):
        return p
    # 去掉开头的 ./ 或 .\，再基于 PROJECT_ROOT 拼接
    norm = p.replace("\\", "/").lstrip("./")
    return str((PROJECT_ROOT / norm).resolve())


def bundled_tool(tool: str) -> str:
    """打包内外部工具：<PROJECT_ROOT>/third_party/<tool>/<tool>.exe。

    源码态：项目根 third_party/<tool>/（build.ps1 打包数据源，gitignore）；
    打包态（sys.frozen）：PROJECT_ROOT = exe 目录，资源经 build.ps1 后处理
    落在 dist/third_party/<tool>/。存在返回绝对路径，否则返回 ""（调用方保持降级）。
    """
    try:
        exe = str(PROJECT_ROOT / "third_party" / tool / f"{tool}.exe")
        return exe if os.path.isfile(exe) else ""
    except Exception:
        return ""


def find_llama_server(asr_config: dict) -> str:
    """llama-server 查找链：
    1. 配置 asr.llama_server（settings.yaml，空则不指定）
    2. PATH：shutil.which("llama-server")
    3. LM Studio 扩展目录：~/.lmstudio/extensions/backends/*/llama-server.exe（取最近安装）
    找不到返回 ""（调用方保持降级，不抛异常）。
    """
    path = ((asr_config or {}).get("llama_server") or "").strip()
    if path and os.path.isfile(path):
        return path
    found = shutil.which("llama-server")
    if found:
        return found
    cands = glob.glob(
        os.path.expanduser(r"~/.lmstudio/extensions/backends/*/llama-server.exe")
    )
    if cands:
        return max(cands, key=os.path.getmtime)
    return ""
