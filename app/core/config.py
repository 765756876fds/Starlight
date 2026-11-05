"""
全局配置加载器
P2（2026-10-18）：配置缺失不再崩溃——使用内置默认配置并自动写盘；
相对路径（./data、./logs、./data/music.db 等）基于 PROJECT_ROOT 绝对化，消除 CWD 依赖。
"""
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from app.version import get_version

from app.core.path_resolve import (
    PROJECT_ROOT,
    USER_DATA_DIR,
    bundled_tool,
    migrate_legacy_user_data,
    resolve_under_root,
)

# 相对路径键 → 绝对化（解析后写入 self._data，避免下游各自猜）
_RELATIVE_KEYS = (
    "app.data_dir",
    "app.log_dir",
    "music.database",
    "player.database",
    "tts.output_dir",
)


def _default_config() -> dict[str, Any]:
    """内置默认配置（全新机器无配置文件时的兜底，路径全部基于 USER_DATA_DIR 绝对化）。
    R1: frozen 态用户数据走 %LOCALAPPDATA%\Starlight；源码态 USER_DATA_DIR == PROJECT_ROOT。
    """
    data_dir = str(USER_DATA_DIR / "data")
    logs_dir = str(USER_DATA_DIR / "logs")
    # 打包内 mpv 优先（dist/third_party/mpv/mpv.exe）；源码态回退 "mpv"（PATH 查找）
    default_mpv = bundled_tool("mpv") or "mpv"
    return {
        "app": {
            "name": "Starlight",
            "version": get_version(),
            "log_level": "INFO",
            "data_dir": data_dir,
            "log_dir": logs_dir,
        },
        "music": {
            "scan_dirs": [],
            "extensions": [".mp3", ".flac", ".wav", ".m4a", ".ogg", ".ape"],
            "database": str(USER_DATA_DIR / "data" / "music.db"),
            "download_dir": "",
            "mpv_path": default_mpv,
            "mpv_ipc_pipe": r"\\.\pipe\lol_music_mpv",
            "default_volume": 70,
            "queue_size": 50,
            "play_mode": "sequential",
        },
        # 顶层 mpv_path 兼容（music/controller、speech/tts 读无前缀键名的调用方）
        "mpv_path": default_mpv,
        "player": {
            "enabled": True,
            "database": str(USER_DATA_DIR / "data" / "player.db"),
            "cache_hours": 24,
            "lobby_preload": True,
        },
        "tts": {
            "enabled": True,
            "engine": "edge",
            "edge_voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "volume": "+0%",
            "output_dir": str(USER_DATA_DIR / "data" / "tts"),
            "qwen_api_key": "",
            "qwen_base_url": "https://dashscope.aliyuncs.com/api/v1",
            "qwen_model": "qwen-audio-3.1-tts-flash",
            "qwen_voice": "longanlingxin_v3.1",
            "qwen_rate": 1.0,
        },
        "asr": {
            "enabled": True,  # 第二刀：总开关；false 时不下载模型/不拉起 llama-server/不轮询
            "engine": "qwen3_server",
            "bootstrap_enabled": True,  # 轻量版：模型缺失时自动下载（hf-mirror）+ 拉起 llama-server
            "llama_backend": "auto",  # R2: auto/vulkan/cpu
            "llama_ctx": 8192,  # R2: 默认上下文窗口
            "qwen3_server": {
                "base_url": "http://127.0.0.1:8080/v1",
                "model": "default",
                "timeout": 30.0,
            },
            "qwen3_asr_model": "",
            "qwen3_asr_mmproj": "",
            "vad_enabled": True,
            "vad_silence_duration": 500,
            "sample_rate": 16000,
            "ptt_key": None,
        },
        "lcu": {
            "enabled": True,
            "reconnect_interval": 5,
            "process_name": "LeagueClientUx.exe",
            "lockfile_path": None,
        },
        "live_client": {
            "enabled": True,
            "base_url": "https://127.0.0.1:2999",
            "poll_interval": 1.0,
            "verify_ssl": False,
        },
        "game_alarm": {
            "enabled": True,
            "rift_herald_lead": 60,
            "dragon_lead": 40,
            "baron_lead": 40,
            "flash_cooldown": 300,
            "ward_duration": 120,
            "tolerance": 10,
        },
        "speech_judge": {"enabled": True, "judge_words": []},
        "online_music": {"demo_source": False, "archive_source": False},
        "automation": {"enabled": False},
        "combos": {"enabled": False},
    }


def _absolutize(data: dict[str, Any]) -> dict[str, Any]:
    """把已知相对路径键（点号分隔）绝对化。缺失的键不动。"""
    out = data
    for key in _RELATIVE_KEYS:
        parts = key.split(".")
        cur = out
        for p in parts[:-1]:
            if not isinstance(cur, dict) or p not in cur:
                cur = None
                break
            cur = cur[p]
        if cur is None or not isinstance(cur, dict):
            continue
        val = cur.get(parts[-1])
        if isinstance(val, str) and val:
            cur[parts[-1]] = resolve_under_root(val)
    return out


def resolve_mpv_path(configured: str | None) -> str:
    """mpv 可执行文件查找链（打包零配置运行，2026-10-18）：

    1. settings.yaml 用户显式配置且文件存在 → 用之（尊重用户路径）
    2. 打包内置 PROJECT_ROOT/third_party/mpv/mpv.exe（bundled_tool("mpv")，
       源码态 = 项目根 third_party，打包态 = dist/exe 同目录 third_party）
    3. 其余（configured 原值或 "mpv"）交给下游 mpv.py:_resolve_mpv_path 的
       PATH（shutil.which）级兜底；仍无 → 各调用方打印提示。

    覆盖"全新电脑无 settings.yaml"（走 _default_config 的 bundled）与
    "settings.yaml 存在但缺 mpv_path 键"（此处 bundled 兜底）两种场景。
    """
    if configured:
        p = str(configured).strip()
        if p and os.path.isfile(p):
            return p
    bundled = bundled_tool("mpv")
    if bundled:
        return bundled
    return (str(configured or "").strip()) or "mpv"


class Config:
    """单例配置管理器"""

    _instance: Optional["Config"] = None
    _data: dict[str, Any] = {}

    def __new__(cls, config_path: str | None = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load(config_path)
        return cls._instance

    @classmethod
    def _reset_instance(cls):
        """测试用：重置单例（避免跨用例污染）。"""
        cls._instance = None
        cls._data = {}

    def _load(self, config_path: str | None = None):
        """加载配置文件；不存在 → 使用内置默认配置并自动写盘（不抛异常）。
        R1: 默认 settings.yaml 路径 = USER_DATA_DIR/config/settings.yaml（frozen 态在 LOCALAPPDATA）。
        R1 补充: 加载前先尝试迁移旧 Program Files 下的用户数据。
        """
        migrate_legacy_user_data()  # 迁移旧配置/数据库到用户目录（幂等，不覆盖）
        if config_path is None:
            config_path = str(USER_DATA_DIR / "config" / "settings.yaml")

        config_path = Path(config_path)
        if not config_path.exists():
            # 兜底：内置默认配置 + 自动落盘（用户可后续编辑）
            self._data = _default_config()
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)
            except Exception as e:
                print(f"[Config] 默认配置写盘失败（继续用内存默认）: {e}")
        else:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    self._data = yaml.safe_load(f) or {}
            except Exception as e:
                # 配置文件损坏，备份原文件，使用默认配置
                import shutil
                import time
                bak_path = str(config_path) + f".bak_{int(time.time())}"
                try:
                    shutil.copy2(config_path, bak_path)
                except Exception:
                    pass
                print(f"[Config] 配置文件损坏，已备份至 {bak_path}，将使用默认配置恢复。错误: {e}")
                self._data = _default_config()
                # 恢复后自动重新写一份干净的
                try:
                    with open(config_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)
                except Exception:
                    pass
        # 相对路径键 → 基于 PROJECT_ROOT 绝对化（不依赖 CWD）
        self._data = _absolutize(dict(self._data))

        # P0-B: mpv_path 失效自动回退——用户配置指向不存在的旧路径时，回退到打包内置 mpv
        bundled_mpv = bundled_tool("mpv")
        for mpv_key in ("music.mpv_path", "mpv_path"):
            cur = self.get(mpv_key, "")
            if cur and not os.path.isfile(str(cur)):
                if bundled_mpv:
                    print(f"[Config] mpv_path 失效（{cur} 不存在），回退到内置 {bundled_mpv}")
                    # 写回 _data
                    parts = mpv_key.split(".")
                    node = self._data
                    for p in parts[:-1]:
                        node = node[p]
                    node[parts[-1]] = bundled_mpv

        # 确保数据/日志目录存在
        data_dir = self.get("app.data_dir", str(USER_DATA_DIR / "data"))
        log_dir = self.get("app.log_dir", str(USER_DATA_DIR / "logs"))
        for d in (data_dir, log_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:
                print(f"[Config] 目录创建失败 {d}: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值，支持点号分隔的嵌套键
        例如: config.get("music.mpv_path")
        """
        # Old personal settings may still contain a version. It is product
        # metadata, not a user preference; do not rewrite the settings file.
        if key == "app.version":
            return get_version()
        keys = key.split(".")
        value = self._data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def get_section(self, section: str) -> dict[str, Any]:
        """获取整个配置段"""
        return self._data.get(section, {})

    @property
    def data(self) -> dict[str, Any]:
        return self._data.copy()


def get_config(config_path: str | None = None) -> Config:
    """获取全局配置单例"""
    return Config(config_path)
