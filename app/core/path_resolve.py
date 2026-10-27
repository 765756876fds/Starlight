r"""路径解析工具：外部可执行文件查找链（配置 → 自动搜索 → 降级）。
P0 路径自适应（2026-10-16）：llama-server 定位统一走这里；
P1（2026-10-16）：PROJECT_ROOT 单一来源，lol_data / music_sources 路径统一基于它。
P2（2026-10-18）：PROJECT_ROOT 打包态适配（sys.frozen → exe 目录）+
resolve_under_root 统一把相对配置路径绝对化（消除 CWD 依赖）。
R1（2026-10-26）：用户数据目录分离——frozen 态用户可写数据走 %LOCALAPPDATA%\Starlight，
程序资源仍在 exe 目录；源码态两者都在项目目录。
R2（2026-10-26）：llama-server 双后端（vulkan/cpu）查找。
R4（2026-10-27）：music_sources 用户可写目录统一解析 + 随包插件首启种子；
旧版 JSON 用户数据（music_dirs/ui_prefs/music_sources）随 settings.yaml 一并迁移。
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


def _compute_user_data_dir() -> Path:
    r"""用户可写数据目录。
    - frozen 态（安装版）：%LOCALAPPDATA%\Starlight（Program Files 只读，不能写配置/模型/日志）。
    - 源码态（开发/pytest）：项目目录（与 PROJECT_ROOT 相同，开发期不分离）。
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Starlight"
    return _compute_project_root()


# 项目根（只读程序资源）。所有 data/、config/、logs/ 路径的单一来源（不依赖 CWD）。
PROJECT_ROOT = _compute_project_root()

# 用户可写数据根（配置、数据库、日志、模型、下载缓存都在这里）。
USER_DATA_DIR = _compute_user_data_dir()


def user_data_path(*parts: str) -> str:
    """拼接到用户数据目录下并确保父目录存在，返回绝对路径字符串。"""
    p = USER_DATA_DIR.joinpath(*parts)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return str(p)


def migrate_legacy_user_data() -> None:
    """R1 补充：旧安装版用户数据自动迁移（Program Files → LOCALAPPDATA）。

    背景：旧版本把 settings.yaml、music.db、player.db 写在 exe 目录（Program Files）。
    R1 后用户数据改到 USER_DATA_DIR。本函数在 config 加载前自动迁移：
    - 仅 frozen 态执行（源码态 PROJECT_ROOT == USER_DATA_DIR，跳过）。
    - 仅当目标文件不存在时才迁移（不覆盖用户已有新数据）。
    - SQLite 数据库同时拷贝 .db、.db-wal、.db-shm（如果存在）。
    - 不删除源文件（Program Files 下旧文件保留）。
    - 任何失败打印日志，不抛异常，不阻塞启动。
    """
    if not getattr(sys, "frozen", False):
        return  # 源码态不分离，无需迁移
    # 旧位置（exe 目录下的 config/ 和 data/）
    legacy_config = PROJECT_ROOT / "config" / "settings.yaml"
    legacy_music_db = PROJECT_ROOT / "data" / "music.db"
    legacy_player_db = PROJECT_ROOT / "data" / "player.db"
    # 新位置
    new_config = USER_DATA_DIR / "config" / "settings.yaml"
    new_music_db = USER_DATA_DIR / "data" / "music.db"
    new_player_db = USER_DATA_DIR / "data" / "player.db"

    def _copy_if_missing(src: Path, dst: Path, label: str) -> None:
        try:
            if dst.exists():
                return  # 目标已有，不覆盖
            if not src.is_file():
                return  # 源也没有（全新安装）
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"[Migrate] {label}: {src.name} → {dst}")
        except Exception as e:
            print(f"[Migrate] {label} 失败（不阻塞启动）: {e}")

    def _migrate_sqlite(src: Path, dst: Path, label: str) -> None:
        """SQLite 一致性迁移：用 sqlite3 backup API（自动处理 WAL checkpoint）。
        - 目标已存在 → 跳过。
        - 备份后打开目标验证表存在，验证失败删除不完整目标。
        - 不删除源文件。
        """
        try:
            if dst.exists():
                return  # 目标已有有效数据，不覆盖
            if not src.is_file():
                return  # 源也没有
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 先写到临时文件，验证通过后再原子 rename
            import sqlite3
            tmp = dst.with_suffix(dst.suffix + ".migrate.tmp")
            if tmp.exists():
                tmp.unlink()  # 清理上次中断的临时文件
            src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            try:
                dst_conn = sqlite3.connect(str(tmp))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
            # 验证目标数据库能打开 + 有表
            verify_conn = sqlite3.connect(str(tmp))
            try:
                tables = verify_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            finally:
                verify_conn.close()
            if not tables:
                tmp.unlink(missing_ok=True)
                print(f"[Migrate] {label} 验证失败（无表），已删除不完整目标")
                return
            # 原子替换到正式位置
            os.replace(tmp, dst)
            print(f"[Migrate] {label}: {src.name} → {dst}（sqlite backup 一致）")
        except Exception as e:
            print(f"[Migrate] {label} 失败（不阻塞启动，可重试）: {e}")
            # 清理可能的不完整临时文件
            try:
                tmp = dst.with_suffix(dst.suffix + ".migrate.tmp")
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    # 旧 Qwen API Key（Program Files/tools/.qwen_key）→ 用户目录
    legacy_qwen_key = PROJECT_ROOT / "tools" / ".qwen_key"
    new_qwen_key = USER_DATA_DIR / "tools" / ".qwen_key"
    _copy_if_missing(legacy_qwen_key, new_qwen_key, ".qwen_key")

    _copy_if_missing(legacy_config, new_config, "settings.yaml")
    # 旧 JSON 用户数据（音乐扫描目录 / UI 偏好 / 自定义音源）→ 用户目录
    # （旧版 settings_store.py 与 MusicSourceManager 把它们写在 exe 目录 config/）
    for _jname in ("music_dirs.json", "ui_prefs.json", "music_sources.json"):
        _copy_if_missing(
            PROJECT_ROOT / "config" / _jname,
            USER_DATA_DIR / "config" / _jname,
            _jname,
        )
    # SQLite 数据库：用 backup API 一致性迁移（不手动 copy WAL/SHM）
    for db_src, db_dst, label in [
        (legacy_music_db, new_music_db, "music.db"),
        (legacy_player_db, new_player_db, "player.db"),
    ]:
        _migrate_sqlite(db_src, db_dst, label)


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


def lol_data_dir() -> Path:
    """LoL 静态数据目录统一解析（读写共用）。

    - 源码态：PROJECT_ROOT/data/lol_data。
    - frozen 态：优先用户目录（USER_DATA_DIR/data/lol_data，用户更新后的数据），
      如果用户目录不存在 → 回退到程序资源目录（PROJECT_ROOT/data/lol_data，随包初始数据）。
    - 更新方（lol_data_updater）总是写到用户目录。
    - 读取方按此函数解析：用户目录有更新数据就读用户目录，否则读程序资源目录。
    """
    user = USER_DATA_DIR / "data" / "lol_data"
    if getattr(sys, "frozen", False):
        if user.is_dir() and any(user.iterdir()):
            return user  # 用户目录有更新数据，优先读
        # 用户目录为空或不存在 → 回退到程序资源目录（随包初始数据）
        return PROJECT_ROOT / "data" / "lol_data"
    return PROJECT_ROOT / "data" / "lol_data"


def music_sources_dir() -> Path:
    """在线音乐插件/自定义源目录统一解析（读写共用）。

    - 源码态：PROJECT_ROOT/data/music_sources（与 lol_data 同规则）。
    - frozen 态：USER_DATA_DIR/data/music_sources —— Program Files 只读，
      导入/启用插件必须写用户目录；升级安装不会覆盖用户导入的插件。
    - 首次运行由 seed_bundled_music_sources() 把随包插件种到用户目录，
      所以 frozen 态读取方不用像 lol_data 那样回退资源目录。
    """
    if getattr(sys, "frozen", False):
        return USER_DATA_DIR / "data" / "music_sources"
    return PROJECT_ROOT / "data" / "music_sources"


def seed_bundled_music_sources() -> None:
    """frozen 态首启种子：随包插件（exe 目录 data/music_sources）→ 用户目录。

    - 仅 frozen 态执行（源码态两目录相同，跳过）。
    - 只拷贝用户目录缺失的文件：不覆盖用户已有的导入/修改（幂等，可重复执行）。
    - 按顶层子目录（musicfree/ lx/）逐个同步；子目录不存在则跳过。
    - 任何失败打印日志、不抛异常（在线音乐可用性降级而非阻塞启动）。
    """
    if not getattr(sys, "frozen", False):
        return
    seed_root = PROJECT_ROOT / "data" / "music_sources"
    if not seed_root.is_dir():
        return
    user_root = USER_DATA_DIR / "data" / "music_sources"
    try:
        for sub in ("musicfree", "lx"):
            src_dir = seed_root / sub
            if not src_dir.is_dir():
                continue
            dst_dir = user_root / sub
            for f in src_dir.iterdir():
                if not f.is_file():
                    continue
                dst = dst_dir / f.name
                if dst.exists():
                    continue  # 用户已有（含导入/自改），不覆盖
                try:
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                    print(f"[Seed] 内置插件: {f.name} → {dst}")
                except Exception as e:
                    print(f"[Seed] 插件 {f.name} 拷贝失败（跳过）: {e}")
    except Exception as e:
        print(f"[Seed] 内置插件种子失败（不阻塞启动）: {e}")


def find_llama_server(asr_config: dict) -> str:
    """llama-server 查找链（单后端兼容旧调用）：
    1. 配置 asr.llama_server（settings.yaml，空则不指定）
    2. PATH：shutil.which("llama-server")
    3. 打包内置 third_party/llama/vulkan/llama-server.exe（优先 GPU）
    4. 打包内置 third_party/llama/cpu/llama-server.exe（CPU 回退）
    5. LM Studio 扩展目录：~/.lmstudio/extensions/backends/*/llama-server.exe（取最近安装）
    找不到返回 ""（调用方保持降级，不抛异常）。
    """
    path = ((asr_config or {}).get("llama_server") or "").strip()
    if path and os.path.isfile(path):
        return path
    found = shutil.which("llama-server")
    if found:
        return found
    # R2: 打包内置双后端
    for backend in ("vulkan", "cpu"):
        bundled = PROJECT_ROOT / "third_party" / "llama" / backend / "llama-server.exe"
        if bundled.is_file():
            return str(bundled)
    cands = glob.glob(
        os.path.expanduser(r"~/.lmstudio/extensions/backends/*/llama-server.exe")
    )
    if cands:
        return max(cands, key=os.path.getmtime)
    return ""


def find_llama_backend_candidates(asr_config: dict) -> list[dict]:
    """R2: 返回按优先级排序的 llama-server 后端候选列表。

    每项: {"backend": "vulkan"|"cpu"|"lmstudio"|"custom", "exe": "..."}。
    顺序：用户指定 → vulkan 打包内置 → cpu 打包内置 → PATH → LM Studio。
    asr.llama_backend: auto/vulkan/cpu 控制偏好（vulkan/cpu 强制指定）。
    """
    result: list[dict] = []
    want = str((asr_config or {}).get("llama_backend") or "auto").strip().lower()

    # 1. 用户显式指定的可执行文件最高优先
    custom = str((asr_config or {}).get("llama_server") or "").strip()
    if custom and os.path.isfile(custom):
        result.append({"backend": "custom", "exe": custom})

    # 2. 打包内置双后端（按 want 排序）
    bundled_order = []
    if want == "vulkan":
        bundled_order = ["vulkan", "cpu"]
    elif want == "cpu":
        bundled_order = ["cpu", "vulkan"]
    else:  # auto
        bundled_order = ["vulkan", "cpu"]
    for b in bundled_order:
        exe = PROJECT_ROOT / "third_party" / "llama" / b / "llama-server.exe"
        if exe.is_file():
            result.append({"backend": b, "exe": str(exe)})

    # 3. PATH
    on_path = shutil.which("llama-server")
    if on_path:
        result.append({"backend": "path", "exe": on_path})

    # 4. LM Studio
    cands = glob.glob(
        os.path.expanduser(r"~/.lmstudio/extensions/backends/*/llama-server.exe")
    )
    if cands:
        latest = max(cands, key=os.path.getmtime)
        result.append({"backend": "lmstudio", "exe": latest})

    return result
