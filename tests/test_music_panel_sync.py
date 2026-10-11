"""
音乐面板 UI 状态同步测试：
语音命令（随机播放/顺序播放/高潮模式开/关等）改变 controller 状态后，
面板按钮显示必须跟随，不能只依赖用户点击更新。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.ui.panels.music_panel import MusicPanel


class FakeMusic:
    """面板所需的最小 controller 接口"""

    def __init__(self, mode="sequential", hl=False):
        self._mode = mode
        self._hl = hl

    def get_play_mode(self):
        return self._mode

    def is_highlight_mode(self):
        return self._hl

    def get_volume(self):
        return 70

    def get_online_sources(self):
        return []

    def search_local(self, kw=""):
        return []

    def get_current_song(self):
        return None


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_panel_buttons_follow_voice_state(qapp):
    """语音/外部改状态后，播放模式与高潮模式按钮跟随显示。"""
    fake = FakeMusic(mode="shuffle", hl=True)
    panel = MusicPanel(music=fake)
    try:
        panel._sync_state_buttons()
        assert panel.btn_mode.text() == "🔀 随机", "语音随机播放后按钮应显示随机"
        assert panel.btn_highlight.isChecked() is True, "高潮开时按钮应勾选"
        assert panel.btn_highlight.text() == "🎵 高潮模式：开"

        # 外部（语音）再改为顺序 + 高潮关
        fake._mode = "sequential"
        fake._hl = False
        panel._sync_state_buttons()
        assert panel.btn_mode.text() == "🔁 顺序"
        assert panel.btn_highlight.isChecked() is False
        assert panel.btn_highlight.text() == "🎵 高潮模式：关"
    finally:
        panel._timer.stop()


def test_panel_buttons_no_music_no_crash(qapp):
    """无 music 实例时同步函数不崩溃。"""
    panel = MusicPanel(music=None)
    try:
        panel._sync_state_buttons()
    finally:
        panel._timer.stop()


def test_refresh_syncs_buttons_with_real_controller(qapp, tmp_path):
    """真实 controller 路径：语音命令改状态后，_refresh（完整路径，含 mpv mock）
    必须同步按钮——防止同步被前面的进度/歌曲逻辑异常拖累。"""
    from unittest.mock import MagicMock, patch
    from app.music.controller import MusicController
    from app.music.database import MusicDatabase

    db_path = str(tmp_path / "panel_real.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.mpv.get_live_playback_time.return_value = 0.0
    ctrl.mpv.get_duration.return_value = 10.0

    # 模拟语音命令：随机播放 + 高潮模式开
    ctrl.set_play_mode("shuffle")
    ctrl.set_highlight_mode(True)

    panel = MusicPanel(music=ctrl)
    try:
        panel._refresh()
        assert panel.btn_mode.text() == "🔀 随机", "_refresh 应同步播放模式按钮"
        assert panel.btn_highlight.isChecked() is True, "_refresh 应同步高潮勾选"
        assert panel.btn_highlight.text() == "🎵 高潮模式：开"

        # 再模拟语音：顺序播放 + 高潮模式关
        ctrl.set_play_mode("sequential")
        ctrl.set_highlight_mode(False)
        panel._refresh()
        assert panel.btn_mode.text() == "🔁 顺序"
        assert panel.btn_highlight.isChecked() is False
        assert panel.btn_highlight.text() == "🎵 高潮模式：关"
    finally:
        panel._timer.stop()
    db.close()


def test_refresh_scroll_no_attribute_error(qapp, tmp_path, capsys):
    """回归：_refresh 在"当前歌曲在本地列表中有匹配项"时走 scrollToItem 分支，
    不允许抛 AttributeError（此前 self.list_local.PositionAtCenter 是错误属性，
    导致刷新每轮刷屏错误、并拖累后面的按钮同步）。"""
    from unittest.mock import MagicMock, patch
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    from app.music.controller import MusicController
    from app.music.database import MusicDatabase

    db_path = str(tmp_path / "panel_scroll.db")
    db = MusicDatabase(db_path)
    cfg = {
        "database": db_path,
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"):
        ctrl = MusicController(cfg)
    ctrl.mpv = MagicMock()
    ctrl.mpv.get_live_playback_time.return_value = 5.0
    ctrl.mpv.get_duration.return_value = 200.0

    song = {"id": 1, "title": "测试歌", "artist": "测试", "online": False, "path": "x.flac"}
    ctrl._current_song = song

    panel = MusicPanel(music=ctrl)
    try:
        # 手动填充本地列表，使当前歌曲能在列表中找到匹配项
        item = QListWidgetItem("测试歌 - 测试")
        item.setData(Qt.UserRole, song)
        panel.list_local.addItem(item)

        panel._refresh()

        captured = capsys.readouterr().out
        assert "music refresh error" not in captured, f"_refresh 不应刷屏异常:\n{captured}"
        assert "sync buttons error" not in captured
    finally:
        panel._timer.stop()
    db.close()
