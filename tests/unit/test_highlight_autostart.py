"""启动时自动触发 highlight worker 的回归测试。"""
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit


def test_start_triggers_highlight_background(tmp_path):
    """controller.start() 成功路径应调用 scanner.start_highlight_background()。"""
    from app.music.controller import MusicController
    db_path = str(tmp_path / "t.db")
    cfg = {
        "database": db_path, "scan_dirs": [], "extensions": [".flac"],
        "mpv_path": "mpv", "mpv_ipc_pipe": r"\\.\pipe\test_hl_start",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController") as MockMpv:
        mpv_instance = MagicMock()
        mpv_instance.start.return_value = True
        MockMpv.return_value = mpv_instance
        ctrl = MusicController(cfg)
        # 替换 scanner，监视 start_highlight_background
        ctrl.scanner = MagicMock()
        ctrl.start()
        assert ctrl.scanner.start_highlight_background.called, \
            "start() 应触发后台 highlight worker"


def test_start_mpv_unavailable_also_starts_worker(tmp_path):
    """mpv 不可用降级路径也要启动 worker（highlight 不依赖 mpv）。"""
    from app.music.controller import MusicController
    db_path = str(tmp_path / "t2.db")
    cfg = {
        "database": db_path, "scan_dirs": [], "extensions": [".flac"],
        "mpv_path": "mpv", "mpv_ipc_pipe": r"\\.\pipe\test_hl_start2",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController") as MockMpv:
        mpv_instance = MagicMock()
        mpv_instance.start.return_value = False  # mpv 启动失败
        MockMpv.return_value = mpv_instance
        ctrl = MusicController(cfg)
        ctrl.scanner = MagicMock()
        ok = ctrl.start()
        assert ok is False
        assert ctrl.scanner.start_highlight_background.called, \
            "mpv 不可用也应启动 highlight worker"


def test_highlight_worker_already_running_guard(tmp_path):
    """scanner.start_highlight_background 已有 _highlight_running 防重入。"""
    from app.music.scanner import MusicScanner
    from app.music.database import MusicDatabase
    db = MusicDatabase(str(tmp_path / "t3.db"))
    sc = MusicScanner(db, [], [".flac"], "mpv")
    # 手动置 running=True，再调应直接 return，不重复启动线程
    sc._highlight_running = True
    sc.start_highlight_background()  # 不应抛异常
    assert sc._highlight_running is True
    db.close()


def test_start_highlight_worker_failure_does_not_crash(tmp_path):
    """worker 启动抛异常时，controller.start() 不应崩。"""
    from app.music.controller import MusicController
    db_path = str(tmp_path / "t4.db")
    cfg = {
        "database": db_path, "scan_dirs": [], "extensions": [".flac"],
        "mpv_path": "mpv", "mpv_ipc_pipe": r"\\.\pipe\test_hl_start4",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController") as MockMpv:
        mpv_instance = MagicMock()
        mpv_instance.start.return_value = True
        MockMpv.return_value = mpv_instance
        ctrl = MusicController(cfg)
        ctrl.scanner = MagicMock()
        ctrl.scanner.start_highlight_background.side_effect = RuntimeError("boom")
        # 不应抛
        ok = ctrl.start()
        assert ok is True
