"""状态记忆 + 本地列表跳回优化测试（批 3b 体验优化）。

覆盖：
- 音量防抖写回：滑块松开 → 延迟 1s 写 settings.yaml music.default_volume（一次落盘）
- 播放模式记忆：构造读 config play_mode 恢复；set_play_mode 写回配置
- 电影播放窗口尺寸：QSettings 保存/恢复（closeEvent 存，构造读）
- 本地列表跳回：refresh 只高亮不滚动；用户操作重置 10s 计时；超时单次滚回
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest
pytestmark = pytest.mark.ui

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QListWidgetItem


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeMusic:
    """MusicPanel 构造所需最小接口（带可变当前歌/音量）。"""

    def __init__(self):
        self._current = None
        self._volume = 70

    def get_history(self, limit=200):
        return []

    def get_play_mode(self):
        return "sequential"

    def is_highlight_mode(self):
        return False

    def get_volume(self):
        return self._volume

    def get_online_sources(self):
        return []

    def search_local(self, kw=""):
        return []

    def get_current_song(self):
        return self._current

    def clear_history(self):
        pass

    def play_history_item(self, item):
        return True

    def set_volume(self, v):
        self._volume = int(v)

    def set_play_mode(self, mode):
        pass


# ---------- 音量防抖写回 ----------

class TestVolumeMemory:
    def _panel(self, qapp):
        from app.ui.panels.music_panel import MusicPanel
        return MusicPanel(music=FakeMusic())

    def test_release_schedules_debounced_save(self, qapp, monkeypatch):
        """滑块松开 → 调度 1s 防抖写回（不立即写，拖动过程不落盘）。"""
        from app.ui import settings_store
        captured = []
        monkeypatch.setattr(
            "app.ui.panels.music_panel.QTimer.singleShot",
            lambda ms, cb: captured.append((ms, cb)),
        )
        monkeypatch.setattr(
            settings_store, "save_settings_yaml",
            lambda upd: captured.append(("save", upd)),
        )
        panel = self._panel(qapp)
        panel.slider_vol.setValue(80)            # 拖动实时 set_volume（不写盘）
        panel._on_volume_released()
        assert captured, "singleShot 应被调度"
        ms, cb = captured[0]
        assert ms == 1000
        cb()                                     # 1s 后落盘
        saves = [c for c in captured if isinstance(c, tuple) and c[0] == "save"]
        assert saves and saves[-1][1] == {"music": {"default_volume": 80}}

    def test_release_failure_not_crash(self, qapp, monkeypatch):
        """写回失败 → 打印日志不崩（故障隔离）。"""
        panel = self._panel(qapp)
        monkeypatch.setattr(
            "app.ui.panels.music_panel.QTimer.singleShot",
            lambda ms, cb: (_ for _ in ()).throw(Exception("no timer")),
        )
        panel._on_volume_released()   # 不抛


# ---------- 播放模式记忆 ----------

def _make_controller(tmp_path, play_mode=None):
    from app.music.controller import MusicController
    cfg = {
        "database": str(tmp_path / "t.db"),
        "scan_dirs": [],
        "extensions": [".mp3"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_mem",
        "default_volume": 70,
    }
    if play_mode:
        cfg["play_mode"] = play_mode
    with patch("app.music.controller.MpvController"), \
         patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        c = MusicController(cfg)
    c.mpv = MagicMock()
    return c


class TestPlayModeMemory:
    def test_constructor_restores_saved_mode(self, tmp_path):
        c = _make_controller(tmp_path, "shuffle")
        assert c.queue.get_play_mode() == "shuffle"

    def test_constructor_defaults_sequential(self, tmp_path):
        c = _make_controller(tmp_path)
        assert c.queue.get_play_mode() == "sequential"

    def test_set_play_mode_persists(self, tmp_path, monkeypatch):
        from app.ui import settings_store
        saved = []
        monkeypatch.setattr(settings_store, "save_settings_yaml",
                            lambda upd: saved.append(upd))
        c = _make_controller(tmp_path)
        c.set_play_mode("repeat_all")
        assert c.queue.get_play_mode() == "repeat_all"
        assert saved and saved[-1] == {"music": {"play_mode": "repeat_all"}}


# ---------- 电影播放窗口尺寸记忆（QSettings） ----------

class _FakeCtl:
    def __init__(self):
        self.wid = None
        self.playing = False

    def set_embed_wid(self, wid):
        self.wid = wid

    def play(self, item, index=None):
        return True

    def is_playing(self):
        return self.playing

    def stop(self):
        self.playing = False

    def toggle_pause(self):
        pass

    def tick(self):
        pass


class TestVideoWindowSizeMemory:
    def _make(self, qapp, tmp_path, monkeypatch):
        from app.ui.widgets import video_player_window as vp
        from PySide6.QtCore import QSettings
        # 隔离注册表：重定向到 tmp ini（QSettings 构造签名 (org, app) 兼容）
        ini = str(tmp_path / "qs.ini")
        monkeypatch.setattr(
            vp, "QSettings",
            lambda *a, **k: QSettings(ini, QSettings.IniFormat),
        )
        w = vp.VideoPlayerWindow(_FakeCtl())
        return w

    def test_default_size_960x540(self, qapp, tmp_path, monkeypatch):
        w = self._make(qapp, tmp_path, monkeypatch)
        try:
            assert w.width() == 960 and w.height() == 540
        finally:
            w._timer.stop()

    def test_close_saves_and_reopen_restores(self, qapp, tmp_path, monkeypatch):
        w = self._make(qapp, tmp_path, monkeypatch)
        try:
            w.resize(800, 600)
            w.closeEvent(QCloseEvent())
        finally:
            w._timer.stop()
        w2 = self._make(qapp, tmp_path, monkeypatch)
        try:
            assert (w2.width(), w2.height()) == (800, 600)   # 下次打开恢复
        finally:
            w2._timer.stop()


# ---------- 本地列表"强制跳回"优化 ----------

class TestLocalListJump:
    """自动跳回已彻底剥离：刷新只驱动 Delegate 高亮，绝不滚动/选中/设定时器。"""

    def _panel(self, qapp):
        from app.ui.panels.music_panel import MusicPanel
        music = FakeMusic()
        music.mpv = MagicMock()
        music.mpv.get_live_playback_time.return_value = 5.0
        music.mpv.get_duration.return_value = 10.0
        panel = MusicPanel(music=music)
        for sid, t in ((1, "A"), (2, "B"), (3, "C")):
            it = QListWidgetItem(t)
            it.setData(Qt.UserRole, {"id": sid})
            panel.list_local.addItem(it)
        return panel

    def test_no_jump_timer_initialized(self, qapp):
        """初始化后不存在 _local_jump_timer / _local_anchor_row（彻底剥离）。"""
        panel = self._panel(qapp)
        assert not hasattr(panel, "_local_jump_timer")
        assert not hasattr(panel, "_local_anchor_row")
        assert not hasattr(panel, "_local_jump_to_current")
        # 类自身未重写 eventFilter（基类 QWidget 方法不算）
        assert "eventFilter" not in panel.__class__.__dict__

    def test_refresh_no_scroll_no_select(self, qapp):
        """刷新播放状态：不 scrollToItem、不 setCurrentRow（currentRow 保持未选中）。"""
        panel = self._panel(qapp)
        panel._music._current = {"id": 2, "title": "B", "artist": "", "online": False}
        with patch.object(panel.list_local, "scrollToItem") as sc, \
             patch.object(panel.list_local, "setCurrentRow") as sr:
            panel._refresh()
        sc.assert_not_called()
        sr.assert_not_called()
        assert panel.list_local.currentRow() == -1

    def test_playing_key_still_synced(self, qapp):
        """播放态高亮仍由 Delegate playing_key 驱动（唯一高亮来源）。"""
        panel = self._panel(qapp)
        panel._music._current = {"id": 2, "title": "B", "artist": "", "online": False}
        panel._refresh()
        assert panel._dl_local.playing_key is not None
        assert panel._dl_results.playing_key is not None
