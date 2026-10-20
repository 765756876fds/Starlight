"""设置页与音乐目录的持久化存储（纯文件读写，无 Qt 依赖，带锁）。

- config/music_dirs.json：音乐扫描目录（路径/启用/格式过滤/排除子目录）
- config/ui_prefs.json：前台门 / 装备提醒 / 界面刷新间隔

settings.yaml 只由 TTS 设置页更新 tts 节（音色/语速/音量）。
"""
import json
import os
import threading

_LOCK = threading.RLock()  # 可重入：save_ui_prefs 内部调 load_ui_prefs（同锁），Lock 会死锁
try:
    from app.core.path_resolve import PROJECT_ROOT as _ROOT  # 打包态: exe 目录（配置可写）
except ImportError:
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


def reset_ui_prefs() -> bool:
    """恢复 ui_prefs.json 到默认值（仅设置页管理的字段）。"""
    with _LOCK:
        return _write_json(UI_PREFS_PATH, dict(DEFAULT_PREFS))


def reset_music_dirs() -> bool:
    """删除 music_dirs.json，下次 load_music_dirs() 从 settings.yaml 重新种子。"""
    with _LOCK:
        try:
            if os.path.exists(MUSIC_DIRS_PATH):
                os.remove(MUSIC_DIRS_PATH)
            return True
        except Exception as e:
            print(f"[Settings] music_dirs.json 重置失败: {e}")
            return False


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


# ---------- 桌面歌词偏好（settings.yaml music.lyrics 子段） ----------

DEFAULT_LYRICS_PREFS = {
    "font": "Microsoft YaHei",   # 字体
    "font_size": 24,             # 字号
    "color": "#FAB446",          # 当前行颜色（橙，对齐项目主色 250,180,70）
    "bg_opacity": 0,             # 背景透明度 0-100（0=纯透明不画背景；100=半透黑底）
    "mode": "triple",            # triple=3 行上下文 / single=单行
    "locked": True,              # 锁定=鼠标穿透不可拖（默认锁定）；False=解锁可拖
    "pos_x": None,               # 窗口位置（None=未保存/居中）
    "pos_y": None,
}


def load_lyrics_prefs() -> dict:
    """读 settings.yaml music.lyrics 子段，缺省合并默认值（配置损坏 → 全默认）。"""
    with _LOCK:
        try:
            import yaml
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            raw = cfg.get("music", {}).get("lyrics", {}) or {}
        except Exception:
            raw = {}
        merged = dict(DEFAULT_LYRICS_PREFS)
        if isinstance(raw, dict):
            merged.update({k: raw[k] for k in DEFAULT_LYRICS_PREFS if k in raw})
        return merged


def save_lyrics_prefs(update: dict) -> bool:
    """更新 music.lyrics 子段（仅 DEFAULT_LYRICS_PREFS 内的键）。

    **深合并**：先读当前 lyrics（含已保存的 pos_x/pos_y 等），再叠加本次更新——
    设置页普通改动（collect 不含 pos）绝不能抹掉拖拽保存的位置。
    （save_settings_yaml 只浅合并到 music 层，lyrics 子段整段替换——
    若直接传 clean 会把 yaml 里的 pos 冲掉 → 下次 load pos=None → 窗口 recenter。）
    """
    clean = {k: v for k, v in (update or {}).items() if k in DEFAULT_LYRICS_PREFS}
    if not clean:
        return False
    cur = load_lyrics_prefs()
    cur.update(clean)
    return save_settings_yaml({"music": {"lyrics": cur}})
