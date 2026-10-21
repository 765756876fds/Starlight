"""P1：set_play_mode 双定义收敛（方案 A 删死代码）验收。"""
import pytest

pytestmark = pytest.mark.unit


import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

from app.music.controller import MusicController

CTRL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "app", "music", "controller.py")


def _make_ctrl(tmp_path):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / "p1.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_p1",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.db = db
    return ctrl


class TestP1SetPlayMode:
    def test_set_play_mode_single_definition(self):
        """源码层面：set_play_mode 只剩 1 处定义，死代码（state_store.play_mode 写入）已删。"""
        with open(CTRL_PATH, "r", encoding="utf-8-sig") as f:
            src = f.read()
        assert src.count("def set_play_mode") == 1
        assert 'state_store.set(StateNamespace.MUSIC, "play_mode", mode)' not in src

    def test_play_mode_set_get_works(self, tmp_path):
        """功能层面：设置/读取播放模式正常，queue 同步生效。"""
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_play_mode("shuffle")
        assert ctrl.get_play_mode() == "shuffle"
        assert ctrl.queue.get_play_mode() == "shuffle"

        ctrl.set_play_mode("repeat_one")
        assert ctrl.get_play_mode() == "repeat_one"
        assert ctrl.queue.get_play_mode() == "repeat_one"

    def test_cmd_set_play_mode_entry(self, tmp_path):
        """命令入口 _cmd_set_play_mode 调生效版 set_play_mode。"""
        ctrl = _make_ctrl(tmp_path)
        from app.core.command_bus import Command, CommandType
        ctrl._cmd_set_play_mode(Command(command_type=CommandType.MUSIC_SET_PLAY_MODE, params={"mode": "repeat_all"}))
        assert ctrl.get_play_mode() == "repeat_all"
