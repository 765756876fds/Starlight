"""mpv 启动失败降级测试（修1：mpv 未装/路径错不崩主程序 + PATH 解析）。"""
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

from app.music.mpv import MpvController


def _patch_launch():
    """把 Popen 与管道等待全部 mock 掉，专注 start() 分支逻辑。"""
    return patch.object(MpvController, "_wait_for_pipe", return_value=None)


class TestStartFallback:
    def test_missing_path_returns_false(self):
        """路径不存在且 PATH 解析不到 → start() 返回 False（不抛异常）。"""
        ctl = MpvController("nonexistent_mpv_xyz", r"\\.\pipe\test1")
        with patch.object(shutil, "which", return_value=None):
            ok = ctl.start()
        assert ok is False
        assert ctl._running is False

    def test_resolves_path_via_which(self, tmp_path):
        """PATH 里有 mpv → _resolve_mpv_path 用 shutil.which 找到并更新 mpv_path。"""
        fake = str(tmp_path / "fake_mpv.exe")
        open(fake, "w").close()
        ctl = MpvController("mpv", r"\\.\pipe\test2")
        with patch.object(shutil, "which", return_value=fake), \
             patch("app.music.mpv.subprocess.Popen") as popen, \
             _patch_launch(), \
             patch.object(MpvController, "_init_properties"):
            ok = ctl.start()
        assert ok is True
        assert ctl.mpv_path == fake  # 已解析到 PATH 里的真实路径
        assert popen.called

    def test_start_success_returns_true(self, tmp_path):
        """配置路径真实存在（isfile）→ 直接走 _launch 成功。"""
        fake = str(tmp_path / "mpv.exe")
        open(fake, "w").close()
        ctl = MpvController(fake, r"\\.\pipe\test3")
        with patch("app.music.mpv.subprocess.Popen") as popen, \
             _patch_launch(), \
             patch.object(MpvController, "_init_properties"):
            ok = ctl.start()
        assert ok is True
        assert ctl._running is True
        assert popen.called

    def test_launch_failure_returns_false(self, tmp_path):
        """Popen 抛异常 → _launch 捕获 → start 返回 False，进程/管道清空。"""
        fake = str(tmp_path / "mpv.exe")
        open(fake, "w").close()
        ctl = MpvController(fake, r"\\.\pipe\test4")
        with patch("app.music.mpv.subprocess.Popen", side_effect=OSError("boom")):
            ok = ctl.start()
        assert ok is False
        assert ctl._running is False
        assert ctl._process is None

    def test_taskkill_false_skips_taskkill(self, tmp_path):
        """taskkill=False（自动重启路径）不调 taskkill 杀 mpv.exe（保护 TTS 播报进程）。"""
        fake = str(tmp_path / "mpv.exe")
        open(fake, "w").close()
        ctl = MpvController(fake, r"\\.\pipe\test5")
        with patch("app.music.mpv.subprocess.run") as run, \
             patch("app.music.mpv.subprocess.Popen"), \
             _patch_launch(), \
             patch.object(MpvController, "_init_properties"):
            ok = ctl.start(taskkill=False, pipe_timeout=3.0, prop_timeout=0.5)
        assert ok is True
        run.assert_not_called()
