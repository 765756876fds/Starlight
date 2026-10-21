"""mpv 查找链测试（打包零配置运行）：
settings 显式配置(isfile) → 打包内置 bundled_tool("mpv") → PATH 兜底值。
覆盖 movie/music/tts 三入口共用 resolve_mpv_path 的行为。
"""
import pytest

pytestmark = pytest.mark.unit

import os
import sys

import pytest

from app.core import config as config_mod
from app.core.config import resolve_mpv_path


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x")
    return path


class TestResolveMpvPath:
    def test_configured_exists_wins(self, tmp_path):
        exe = _touch(str(tmp_path / "my_mpv.exe"))
        assert resolve_mpv_path(exe) == exe

    def test_configured_missing_falls_to_bundled(self, tmp_path, monkeypatch):
        bundled = _touch(str(tmp_path / "bundled_mpv.exe"))
        monkeypatch.setattr(config_mod, "bundled_tool", lambda tool: bundled if tool == "mpv" else "")
        assert resolve_mpv_path("C:/not/exists/mpv.exe") == bundled

    def test_configured_none_falls_to_bundled(self, tmp_path, monkeypatch):
        bundled = _touch(str(tmp_path / "bundled_mpv.exe"))
        monkeypatch.setattr(config_mod, "bundled_tool", lambda tool: bundled if tool == "mpv" else "")
        assert resolve_mpv_path(None) == bundled

    def test_all_missing_returns_mpv_placeholder(self, monkeypatch):
        """无配置、无 bundled → 返回 "mpv"（交给下游 mpv.py 的 PATH which 级兜底）。"""
        monkeypatch.setattr(config_mod, "bundled_tool", lambda tool: "")
        assert resolve_mpv_path(None) == "mpv"
        assert resolve_mpv_path("") == "mpv"

    def test_configured_empty_string_falls_to_bundled(self, tmp_path, monkeypatch):
        bundled = _touch(str(tmp_path / "b.mpv.exe"))
        monkeypatch.setattr(config_mod, "bundled_tool", lambda tool: bundled if tool == "mpv" else "")
        assert resolve_mpv_path("   ") == bundled


class TestMovieControllerIntegration:
    def test_movie_controller_uses_bundled_when_config_missing_key(self, tmp_path, monkeypatch):
        """settings.yaml 存在但缺 music.mpv_path 键 → movie 链仍落到打包内置 mpv。"""
        from app.movie.movie_controller import MovieController
        from app.music.mpv import MpvController

        bundled = _touch(str(tmp_path / "mpv.exe"))
        monkeypatch.setattr(config_mod, "bundled_tool", lambda tool: bundled if tool == "mpv" else "")

        class FakeConfig:
            def get(self, key, default=None):
                return default  # 所有键缺失（模拟精简 settings.yaml）

        ctl = MovieController.__new__(MovieController)
        # 只测构造中的 mpv_bin 解析逻辑：直接构造 MpvController 结果
        ctl._config = FakeConfig()
        mpv_bin = resolve_mpv_path(ctl._config.get("music.mpv_path"))
        assert mpv_bin == bundled
        mc = MpvController(mpv_bin)
        assert mc.mpv_path == bundled

    def test_music_controller_uses_bundled_when_config_missing_key(self, tmp_path, monkeypatch):
        """music 链：config dict 缺 mpv_path 键 → bundled 兜底（与 tts 同入口）。"""
        bundled = _touch(str(tmp_path / "mpv.exe"))
        monkeypatch.setattr(config_mod, "bundled_tool", lambda tool: bundled if tool == "mpv" else "")
        assert resolve_mpv_path({}.get("mpv_path")) == bundled
