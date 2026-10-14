"""播报 ducking 测试（修4：TTS 播报期间音乐音量降 50%，播报结束恢复）。

修1（实机问题1）：播放状态判断改为实时 get_property（不读 mpv._properties 启动缓存）。
回归：test_duck_when_idle_cache_stale 覆盖"is_idle() 缓存死链"陷阱。
"""
from unittest.mock import MagicMock, patch

from app.core.event_bus import EventType
from app.music.controller import MusicController


def _make_ctrl(tmp_path, db_name="duck.db", idle=False, paused=False, volume=80):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / db_name)
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_duck",
        "default_volume": 70,
        "duck_volume_pct": 50,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()

    def _get_prop(name, timeout=0.5):
        return {"idle-active": idle, "pause": paused, "volume": volume}.get(name, None)

    ctrl.mpv.get_property.side_effect = _get_prop
    ctrl.mpv.get_volume.return_value = volume
    ctrl.db = db
    return ctrl, db


STARTED = EventType.SPEECH_STARTED
FINISHED = EventType.SPEECH_FINISHED


class TestDucking:
    def test_started_ducks_volume(self, tmp_path):
        """播报开始 → 音乐真实播放中 → 音量降到原值 50%。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._on_speech_started(STARTED)
        ctrl.mpv.set_volume.assert_called_once_with(40)  # 80 * 50%
        assert ctrl._duck_active is True
        assert ctrl._duck_original_volume == 80
        db.close()

    def test_finished_restores_volume(self, tmp_path):
        """播报结束 → 当前音量仍是 ducked → 恢复原音量。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._duck_active = True
        ctrl._duck_original_volume = 80
        ctrl.mpv.get_volume.return_value = 40  # 仍处于 duck 状态
        ctrl._on_speech_finished(FINISHED)
        ctrl.mpv.set_volume.assert_called_once_with(80)
        assert ctrl._duck_active is False
        db.close()

    def test_no_duck_when_idle(self, tmp_path):
        """音乐没在播（idle-active 实时 True）→ 播报不 duck（无意义）。"""
        ctrl, db = _make_ctrl(tmp_path, idle=True)
        ctrl._on_speech_started(STARTED)
        ctrl.mpv.set_volume.assert_not_called()
        assert ctrl._duck_active is False
        db.close()

    def test_no_duck_when_paused(self, tmp_path):
        """音乐暂停中（pause 实时 True）→ 播报不 duck。"""
        ctrl, db = _make_ctrl(tmp_path, paused=True)
        ctrl._on_speech_started(STARTED)
        ctrl.mpv.set_volume.assert_not_called()
        assert ctrl._duck_active is False
        db.close()

    def test_duck_when_idle_cache_stale(self, tmp_path):
        """回归：is_idle() 旧缓存误报 True（修1 前的启动缓存死链），
        实时查询是播放中 → 仍必须 duck。"""
        ctrl, db = _make_ctrl(tmp_path, idle=False, paused=False)
        ctrl.mpv.is_idle.return_value = True  # 旧缓存陷阱：修复后此值不再被读
        ctrl._on_speech_started(STARTED)
        ctrl.mpv.set_volume.assert_called_once_with(40)
        assert ctrl._duck_active is True
        db.close()

    def test_no_double_duck(self, tmp_path):
        """连续两次 SPEECH_STARTED → 只 duck 一次（不重复压音量）。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._on_speech_started(STARTED)
        ctrl._on_speech_started(STARTED)
        ctrl.mpv.set_volume.assert_called_once_with(40)
        db.close()

    def test_user_manual_volume_not_overwritten(self, tmp_path):
        """duck 期间用户手动调过音量 → 播报结束不覆盖用户意图。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._duck_active = True
        ctrl._duck_original_volume = 80
        ctrl.mpv.get_volume.return_value = 60  # 用户手动调到 60 ≠ ducked 40
        ctrl._on_speech_finished(FINISHED)
        ctrl.mpv.set_volume.assert_not_called()
        assert ctrl._duck_active is False
        db.close()
