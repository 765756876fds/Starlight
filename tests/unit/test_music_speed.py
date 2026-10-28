"""音乐倍速测试（V1）。

验证 MusicController.set_speed：
- 合法档位透传到 mpv.set_property("speed", ...)；
- 非法档位/非数字拒绝，不调 mpv；
- mpv 不可用时拒绝；
- mpv 调用失败向上抛，不假装成功；
- Controller 记录值与 state_store 同步。
"""
import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from app.music.controller import MusicController


def _make_ctrl(tmp_path, mpv_available=True):
    from app.music.database import MusicDatabase
    db_path = str(tmp_path / "speed.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": r"\\.\pipe\test_speed",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl._mpv_available = mpv_available
    ctrl.db = db
    return ctrl, db


class TestMusicSpeed:
    def test_default_speed_is_1x(self, tmp_path):
        ctrl, db = _make_ctrl(tmp_path)
        assert ctrl.get_speed() == 1.0
        db.close()

    def test_legal_steps_set_property(self, tmp_path):
        ctrl, db = _make_ctrl(tmp_path)
        for rate in (0.5, 1.5, 2.0, 3.0):
            ok = ctrl.set_speed(rate)
            assert ok is True, rate
            ctrl.mpv.set_property.assert_any_call("speed", rate)
            assert ctrl.get_speed() == rate
        db.close()

    def test_illegal_step_rejected(self, tmp_path):
        ctrl, db = _make_ctrl(tmp_path)
        for bad in (1.3, 2.7, 4.0, 0.1, 11.0):
            ok = ctrl.set_speed(bad)
            assert ok is False, bad
        ctrl.mpv.set_property.assert_not_called()
        assert ctrl.get_speed() == 1.0
        db.close()

    def test_non_numeric_rejected(self, tmp_path):
        ctrl, db = _make_ctrl(tmp_path)
        for bad in ("fast", None, [], {}):
            ok = ctrl.set_speed(bad)
            assert ok is False, bad
        ctrl.mpv.set_property.assert_not_called()
        db.close()

    def test_mpv_unavailable_rejected(self, tmp_path):
        ctrl, db = _make_ctrl(tmp_path, mpv_available=False)
        ok = ctrl.set_speed(1.5)
        assert ok is False
        ctrl.mpv.set_property.assert_not_called()
        db.close()

    def test_mpv_error_propagates_not_swallowed(self, tmp_path):
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.mpv.set_property.side_effect = RuntimeError("mpv dead")
        with pytest.raises(RuntimeError):
            ctrl.set_speed(1.5)
        # 失败时 Controller 不记录新值（UI 显示保持旧档）
        assert ctrl.get_speed() == 1.0
        db.close()

    def test_speed_persisted_to_state_store(self, tmp_path):
        ctrl, db = _make_ctrl(tmp_path)
        ctrl.state_store = MagicMock()
        ctrl.set_speed(1.5)
        ctrl.state_store.set.assert_any_call(
            __import__("app.core.state_store", fromlist=["StateNamespace"]).StateNamespace.MUSIC,
            "speed", 1.5)
        db.close()

    def test_speed_reapplied_after_mpv_restart(self, tmp_path):
        """mpv 自愈重启后，Controller 记录的 _speed 被重新 set_property 到新进程。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._speed = 1.5
        ctrl._mpv_available = True
        ctrl._running = True
        ctrl.mpv.stop_process = MagicMock()
        ctrl.mpv.start.return_value = True
        ctrl.mpv.get_property.return_value = True  # idle-active
        ctrl._restart_mpv_worker(restart_song=None)
        ctrl.mpv.set_property.assert_any_call("speed", 1.5)
        db.close()

    def test_speed_default_1x_not_reapplied_after_restart(self, tmp_path):
        """默认 1.0 时重启不重复 set_property（无意义噪音）。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._speed = 1.0
        ctrl._mpv_available = True
        ctrl._running = True
        ctrl.mpv.stop_process = MagicMock()
        ctrl.mpv.start.return_value = True
        ctrl.mpv.get_property.return_value = True
        ctrl._restart_mpv_worker(restart_song=None)
        speed_calls = [c for c in ctrl.mpv.set_property.call_args_list
                       if c.args and c.args[0] == "speed"]
        assert speed_calls == []
        db.close()

    def test_speed_reapply_failure_does_not_crash_or_reset_intent(self, tmp_path):
        """重启后重 apply speed 抛异常：worker 不炸、不阻断歌曲恢复、
        Controller._speed 保留用户意图（不回退 1.0，与 volume 重启行为一致）。"""
        ctrl, db = _make_ctrl(tmp_path)
        ctrl._speed = 1.5
        ctrl._mpv_available = True
        ctrl._running = True
        ctrl.mpv.stop_process = MagicMock()
        ctrl.mpv.start.return_value = True
        ctrl.mpv.get_property.return_value = True

        def _sp(name, *a, **k):
            if name == "speed":
                raise RuntimeError("ipc not ready yet")
        ctrl.mpv.set_property.side_effect = _sp
        # 不应抛到 worker 外层
        ctrl._restart_mpv_worker(restart_song=None)
        # _speed 保留用户意图（不被回退）
        assert ctrl.get_speed() == 1.5
        # worker 正常收尾
        assert ctrl._mpv_restart_pending is False
        db.close()
