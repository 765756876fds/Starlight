"""打包前修整第一批测试：配置兜底 / 路径绝对化 / librosa 惰性 / 个人路径清理 / 死依赖清理。

覆盖（对应验收口径）：
- Config 路径不存在 → 不抛异常、返回默认配置、自动写盘
- 相对路径（database/data_dir/log_dir）→ 基于 PROJECT_ROOT 绝对化，不依赖 CWD
- 打包态 PROJECT_ROOT = exe 目录（mock sys.frozen / sys.executable）
- controller db_path 相对值 → 绝对路径拼接正确（旧数据无缝兼容：CWD=仓库根 时二者同文件）
- highlight_detector 惰性 import：模块加载不引入 librosa；调用时正常执行
- tts / js_source / lx_plugin_runner / requirements / .gitignore 无个人路径与死依赖
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core import path_resolve
from app.core.config import Config, get_config
from app.core.path_resolve import PROJECT_ROOT, resolve_under_root

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    Config._reset_instance()
    yield
    Config._reset_instance()


class TestResolveUnderRoot:
    def test_relative_under_root(self):
        assert resolve_under_root("./data/music.db") == str(
            (REPO_ROOT / "data" / "music.db").resolve()
        )

    def test_bare_relative(self):
        assert resolve_under_root("data/player.db") == str(
            (REPO_ROOT / "data" / "player.db").resolve()
        )

    def test_windows_backslash_relative(self):
        assert resolve_under_root(".\\data\\music.db") == str(
            (REPO_ROOT / "data" / "music.db").resolve()
        )

    def test_absolute_untouched(self):
        assert resolve_under_root(r"D:\music\a.db") == r"D:\music\a.db"

    def test_empty_unchanged(self):
        assert resolve_under_root("") == ""
        assert resolve_under_root(None) is None


class TestProjectRoot:
    def test_source_mode_is_repo_root(self):
        assert PROJECT_ROOT == REPO_ROOT

    def test_frozen_mode_uses_executable_dir(self, monkeypatch):
        monkeypatch.setattr(path_resolve.sys, "frozen", True, raising=False)
        monkeypatch.setattr(path_resolve.sys, "executable", r"C:\app\portable\MusicApp.exe")
        assert path_resolve._compute_project_root() == Path(r"C:\app\portable")

    def test_data_path_independent_of_cwd(self, monkeypatch, tmp_path):
        """核心验收：路径基于程序根目录而非 CWD。模拟从别处启动，PROJECT_ROOT 不变。"""
        monkeypatch.chdir(tmp_path)
        assert path_resolve.PROJECT_ROOT == REPO_ROOT
        assert resolve_under_root("./data/music.db").startswith(str(REPO_ROOT))


class TestConfigFallback:
    def test_missing_config_returns_defaults_and_writes_file(self, tmp_path):
        missing = tmp_path / "nested" / "settings.yaml"
        cfg = Config(str(missing))
        assert cfg.get("app.name") == "LoL Music Agent"
        assert cfg.get("music.database")  # 默认绝对路径
        assert missing.exists()  # 自动写盘

    def test_missing_config_no_raise(self, tmp_path):
        cfg = get_config(str(tmp_path / "no.yaml"))
        assert cfg.get("app.data_dir")  # 不抛异常

    def test_default_paths_absolute(self, tmp_path):
        cfg = Config(str(tmp_path / "settings.yaml"))
        data_dir = cfg.get("app.data_dir")
        log_dir = cfg.get("app.log_dir")
        assert os.path.isabs(data_dir)
        assert os.path.isabs(log_dir)
        assert data_dir.startswith(str(REPO_ROOT))

    def test_relative_values_absolutized(self, tmp_path):
        y = tmp_path / "s.yaml"
        y.write_text(
            "app:\n  data_dir: ./data\n  log_dir: ./logs\n"
            "music:\n  database: ./data/music.db\n"
            "player:\n  database: data/player.db\n",
            encoding="utf-8",
        )
        cfg = Config(str(y))
        assert cfg.get("music.database") == str((REPO_ROOT / "data" / "music.db").resolve())
        assert cfg.get("player.database") == str((REPO_ROOT / "data" / "player.db").resolve())
        assert cfg.get("app.data_dir") == str((REPO_ROOT / "data").resolve())

    def test_absolute_values_untouched(self, tmp_path):
        y = tmp_path / "s.yaml"
        y.write_text("music:\n  database: D:\\custom\\music.db\n", encoding="utf-8")
        cfg = Config(str(y))
        assert cfg.get("music.database") == r"D:\custom\music.db"

    def test_defaults_cover_get_sections(self, tmp_path):
        cfg = Config(str(tmp_path / "s.yaml"))
        assert "database" in cfg.get_section("music")
        assert "lcu" in cfg.get_section("lcu") or cfg.get_section("lcu")  # 各段均可读


class TestControllerDbPath:
    def test_controller_absolutizes_relative_db(self, tmp_path):
        from app.music.controller import MusicController

        cfg = {
            "database": "./data/music.db",
            "scan_dirs": [],
            "extensions": [".mp3"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test_pkg",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"), patch(
            "app.music.controller.MusicDatabase"
        ):
            ctrl = MusicController(cfg)
        assert ctrl.db_path == str((REPO_ROOT / "data" / "music.db").resolve())

    def test_controller_keeps_absolute_db(self, tmp_path):
        from app.music.controller import MusicController

        abs_db = str(tmp_path / "x.db")
        cfg = {
            "database": abs_db,
            "scan_dirs": [],
            "extensions": [".mp3"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test_pkg",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"), patch(
            "app.music.controller.MusicDatabase"
        ):
            ctrl = MusicController(cfg)
        assert ctrl.db_path == abs_db


class TestLibrosaLazy:
    def test_module_import_does_not_load_librosa(self):
        import app.music.highlight_detector as hd
        assert "librosa" not in hd.__dict__

    def test_detect_highlight_works_with_lazy_import(self, monkeypatch):
        """假 librosa（注入 sys.modules）→ detect_highlight 正常走完整算法。"""
        import numpy as np
        import app.music.highlight_detector as hd

        fake = MagicMock()
        sr = 22050
        dur = 30.0
        fake.load.return_value = (np.zeros(sr * 30), sr)
        fake.get_duration.return_value = dur
        # 中央峰值的 RMS：frames_to_time 对应 101 帧
        rms = np.zeros((1, 101))
        rms[0, 50] = 1.0
        fake.feature.rms.return_value = rms
        hop = 512
        fake.frames_to_time.side_effect = lambda n, **kw: np.arange(len(n)) * hop / sr
        monkeypatch.setitem(sys.modules, "librosa", fake)

        result = hd.detect_highlight("dummy.mp3")
        assert result is not None
        start, end, score = result
        assert 0 <= start < end <= dur
        assert score > 0

    def test_failure_path_returns_none(self, monkeypatch):
        import app.music.highlight_detector as hd

        fake = MagicMock()
        fake.load.side_effect = OSError("decoder failed")
        fake.get_duration.side_effect = OSError("decoder failed")
        monkeypatch.setitem(sys.modules, "librosa", fake)
        assert hd.detect_highlight("bad.mp3") is None


class TestPersonalPathCleanup:
    def test_tts_no_legacy_mpv_path(self):
        src = Path("app/speech/tts.py").read_text(encoding="utf-8")
        assert r"D:\新建文件夹" not in src

    def test_js_source_no_drive_d_node_candidates(self):
        src = Path("app/music/online/sources/js_source.py").read_text(encoding="utf-8")
        assert r"D:\nodejs" not in src
        assert r"D:\Program Files\nodejs" not in src
        # env 优先候选仍存在
        assert 'os.environ.get("NODE_EXE"' in src

    def test_lx_plugin_runner_no_dead_main(self):
        src = Path("app/music/lx_plugin_runner.py").read_text(encoding="utf-8")
        assert r"D:\落雪" not in src
        assert "__main__" not in src


class TestDepsCleanup:
    def test_requirements_no_torch(self):
        lines = Path("requirements.txt").read_text(encoding="utf-8").splitlines()
        assert not any("torch" in ln for ln in lines)

    def test_gitignore_covers_fingerprint_db(self):
        gi = Path(".gitignore").read_text(encoding="utf-8")
        assert "data/music/" in gi
