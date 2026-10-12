"""设置页与音乐目录的持久化存储（纯文件读写，无 Qt 依赖，带锁）。

- config/music_dirs.json：音乐扫描目录（路径/启用/格式过滤/排除子目录）
- config/ui_prefs.json：前台门 / 装备提醒 / 界面刷新间隔

settings.yaml 只由 TTS 设置页更新 tts 节（音色/语速/音量）。
"""
import json
import os
import threading

_LOCK = threading.Lock()
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MUSIC_DIRS_PATH = os.path.join(_ROOT, "config", "music_dirs.json")
UI_PREFS_PATH = os.path.join(_ROOT, "config", "ui_prefs.json")
SETTINGS_PATH = os.path.join(_ROOT, "config", "settings.yaml")

ALL_EXTENSIONS = ["mp3", "flac", "wav", "m4a", "ogg", "ape"]

DEFAULT_PREFS = {
    "combo_window_gate": True,   # 仅 LoL 前台时触发（持久化）
    "equip_notify": True,        # 敌方装备语音提醒（持久化）
    "ui_refresh_interval": 500,  # 界面刷新间隔 ms
}


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[Settings] 写入失败 {path}: {e}")
        return False


# ---------- music_dirs.json ----------

def load_music_dirs() -> list:
    """读取音乐目录列表。文件不存在时用 settings.yaml music.scan_dirs 做初始种子。"""
    with _LOCK:
        data = _read_json(MUSIC_DIRS_PATH, None)
        if data is not None and isinstance(data.get("dirs"), list):
            return data["dirs"]
        # 首次：从 settings.yaml 迁移现有目录（只读一次，不写回）
        try:
            import yaml
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            scan_dirs = cfg.get("music", {}).get("scan_dirs") or []
            dirs = [{
                "path": p,
                "enabled": True,
                "extensions": list(ALL_EXTENSIONS),
                "exclude_subdirs": False,
            } for p in scan_dirs]
        except Exception:
            dirs = []
        return dirs


def save_music_dirs(dirs: list) -> bool:
    with _LOCK:
        return _write_json(MUSIC_DIRS_PATH, {"dirs": dirs})


# ---------- ui_prefs.json ----------

def load_ui_prefs() -> dict:
    with _LOCK:
        data = _read_json(UI_PREFS_PATH, {})
        merged = dict(DEFAULT_PREFS)
        if isinstance(data, dict):
            merged.update({k: data[k] for k in DEFAULT_PREFS if k in data})
        return merged


def save_ui_prefs(prefs: dict) -> bool:
    with _LOCK:
        cur = load_ui_prefs()
        cur.update({k: v for k, v in prefs.items() if k in DEFAULT_PREFS})
        return _write_json(UI_PREFS_PATH, cur)


# ---------- settings.yaml（TTS 节更新，带锁） ----------

def save_settings_yaml(update: dict) -> bool:
    """更新 settings.yaml 顶层指定节（如 {"tts": {...}}），保留其他内容。"""
    with _LOCK:
        try:
            import yaml
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for section, values in update.items():
                if not isinstance(values, dict):
                    continue
                cur = data.setdefault(section, {})
                if not isinstance(cur, dict):
                    cur = {}
                    data[section] = cur
                cur.update(values)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            return True
        except Exception as e:
            print(f"[Settings] settings.yaml 写入失败: {e}")
            return False
