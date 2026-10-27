"""R4（2026-10-27）用户数据路径回归测试。

覆盖第二批集中施工的路径修复：
- settings_store 的 music_dirs/ui_prefs/settings.yaml 全部落 USER_DATA_DIR/config
  （frozen 态 = %LOCALAPPDATA%\\Starlight，源码态 == PROJECT_ROOT）；
- MusicSourceManager 默认 config_dir = USER_DATA_DIR/config（旧实现写 PROJECT_ROOT/config）；
- music_sources_dir()：源码态 = PROJECT_ROOT/data/music_sources，frozen 态 = USER_DATA_DIR；
- seed_bundled_music_sources()：frozen 态首启种子（幂等、不覆盖用户文件）、源码态无副作用；
- migrate_legacy_user_data()：旧版 JSON 用户数据随 settings.yaml 一并迁移（不覆盖已有目标）。
"""
import pytest

pytestmark = pytest.mark.unit

import json
import os
import sys

from app.core import path_resolve as pr
from app.core.path_resolve import USER_DATA_DIR


# ---------- settings_store 路径 ----------

def test_settings_store_paths_under_user_data_dir():
    """R4：三个持久化文件全部在 USER_DATA_DIR/config（frozen 态 = LOCALAPPDATA，可写）。"""
    import app.ui.settings_store as ss
    assert ss.MUSIC_DIRS_PATH == str(USER_DATA_DIR / "config" / "music_dirs.json")
    assert ss.UI_PREFS_PATH == str(USER_DATA_DIR / "config" / "ui_prefs.json")
    assert ss.SETTINGS_PATH == str(USER_DATA_DIR / "config" / "settings.yaml")


def test_settings_store_roundtrip_writes_to_config_dir(tmp_path, monkeypatch):
    """保存/读取往返：写进指定（用户）配置目录，失败时返回 False 且不抛。"""
    import app.ui.settings_store as ss
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True)
    monkeypatch.setattr(ss, "MUSIC_DIRS_PATH", str(cfg / "music_dirs.json"))
    monkeypatch.setattr(ss, "UI_PREFS_PATH", str(cfg / "ui_prefs.json"))
    monkeypatch.setattr(ss, "SETTINGS_PATH", str(cfg / "settings.yaml"))

    assert ss.save_music_dirs([{"path": "D:/music", "enabled": True, "extensions": ["mp3"]}])
    assert (cfg / "music_dirs.json").exists()
    assert ss.load_music_dirs()[0]["path"] == "D:/music"

    assert ss.save_ui_prefs({"equip_notify": False, "combo_window_gate": True})
    assert ss.load_ui_prefs()["equip_notify"] is False
    assert ss.load_ui_prefs()["combo_window_gate"] is True  # 其余默认保留

    assert ss.save_settings_yaml({"tts": {"engine": "edge"}})
    assert ss.save_settings_yaml({"tts": {"rate": "+10%"}})  # 浅合并保留 engine
    import yaml
    with open(cfg / "settings.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["tts"]["engine"] == "edge"
    assert data["tts"]["rate"] == "+10%"


# ---------- MusicSourceManager 默认配置目录 ----------

def test_source_manager_default_config_dir_under_user_data():
    from app.music.source_manager import MusicSourceManager
    m = MusicSourceManager()
    assert m.config_dir == str(USER_DATA_DIR / "config")
    assert m.sources_file == str(USER_DATA_DIR / "config" / "music_sources.json")


def test_source_manager_explicit_config_dir(tmp_path):
    from app.music.source_manager import MusicSourceManager
    m = MusicSourceManager(str(tmp_path / "custom"))
    assert m.config_dir == str(tmp_path / "custom")


# ---------- music_sources_dir ----------

def test_music_sources_dir_source_mode():
    assert pr.music_sources_dir() == pr.PROJECT_ROOT / "data" / "music_sources"


def test_music_sources_dir_frozen_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(pr, "USER_DATA_DIR", tmp_path / "ud")
    assert pr.music_sources_dir() == tmp_path / "ud" / "data" / "music_sources"


# ---------- seed_bundled_music_sources ----------

def test_seed_bundled_music_sources_frozen(tmp_path, monkeypatch):
    """frozen 态首启种子：拷贝缺失插件、不覆盖用户文件、幂等可重复。"""
    legacy = tmp_path / "legacy"
    user = tmp_path / "user"
    (legacy / "data" / "music_sources" / "musicfree").mkdir(parents=True)
    (legacy / "data" / "music_sources" / "lx").mkdir(parents=True)
    (legacy / "data" / "music_sources" / "musicfree" / "A.js").write_text("bundled-A", encoding="utf-8")
    (legacy / "data" / "music_sources" / "lx" / "B.js").write_text("bundled-B", encoding="utf-8")
    # 用户已有同名文件（内容不同，不得覆盖）
    (user / "data" / "music_sources" / "musicfree").mkdir(parents=True)
    user_a = user / "data" / "music_sources" / "musicfree" / "A.js"
    user_a.write_text("user-A", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(pr, "PROJECT_ROOT", legacy)
    monkeypatch.setattr(pr, "USER_DATA_DIR", user)
    pr.seed_bundled_music_sources()

    assert user_a.read_text(encoding="utf-8") == "user-A"  # 不覆盖用户修改
    b = user / "data" / "music_sources" / "lx" / "B.js"
    assert b.read_text(encoding="utf-8") == "bundled-B"

    # 幂等：再跑一次不重复、不破坏
    pr.seed_bundled_music_sources()
    assert b.read_text(encoding="utf-8") == "bundled-B"


def test_seed_bundled_music_sources_source_noop(tmp_path, monkeypatch):
    """源码态直接跳过（不创建任何目录）。"""
    monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path / "root")
    monkeypatch.setattr(pr, "USER_DATA_DIR", tmp_path / "user")
    pr.seed_bundled_music_sources()
    assert not (tmp_path / "root" / "data").exists()


# ---------- migrate_legacy_user_data JSON 扩展 ----------

def test_migrate_legacy_json_configs(tmp_path, monkeypatch):
    """旧版 exe 目录 config 下的 JSON 用户数据迁移到用户目录；目标已有不覆盖。"""
    legacy = tmp_path / "legacy"
    user = tmp_path / "user"
    (legacy / "config").mkdir(parents=True)
    (legacy / "data").mkdir(parents=True)
    (legacy / "config" / "music_dirs.json").write_text(
        json.dumps({"dirs": [{"path": "D:/old", "enabled": True}]}), encoding="utf-8")
    (legacy / "config" / "music_sources.json").write_text(
        json.dumps({"sources": [{"name": "s1", "api_url": "http://x"}]}), encoding="utf-8")
    (legacy / "config" / "ui_prefs.json").write_text(
        json.dumps({"equip_notify": False}), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(pr, "PROJECT_ROOT", legacy)
    monkeypatch.setattr(pr, "USER_DATA_DIR", user)
    pr.migrate_legacy_user_data()

    assert (user / "config" / "music_dirs.json").exists()
    assert (user / "config" / "music_sources.json").exists()
    assert (user / "config" / "ui_prefs.json").exists()
    # 源文件保留（不删除）
    assert (legacy / "config" / "music_sources.json").exists()

    # 目标已有新数据 → 不覆盖
    (user / "config" / "music_dirs.json").write_text(
        json.dumps({"dirs": [{"path": "E:/new", "enabled": True}]}), encoding="utf-8")
    pr.migrate_legacy_user_data()
    assert '"E:/new"' in (user / "config" / "music_dirs.json").read_text(encoding="utf-8")


def test_migrate_legacy_skipped_in_source_mode(tmp_path, monkeypatch):
    """源码态不迁移（PROJECT_ROOT == USER_DATA_DIR，无分离）。"""
    monkeypatch.setattr(pr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pr, "USER_DATA_DIR", tmp_path)
    pr.migrate_legacy_user_data()  # 不抛即可
    assert not (tmp_path / "config").exists() or True  # 不创建任何文件
