"""电影内嵌 S5：键盘焦点管理测试（offscreen）。

覆盖：容器 StrongFocus + 点画面收焦点 / 空格播放暂停 / ←→ seek ±5 /
↑↓ 音量 ±5 / F 全屏切换 / M 静音 / Esc 全屏退全屏 / overlay 点击后焦点归还。
"""
import pytest

pytestmark = pytest.mark.ui

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QEvent, QPoint
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])


class _FakeCtl:
    """S5 测试 mock 控制器（记录控制调用）。"""

    def __init__(self):
        self.plays = []
        self.seeks = []
        self.volumes = []
        self.toggles = 0
        self.mutes = 0
        self.playing = True
        self.muted = False

    def set_embed_wid(self, wid):
        pass

    def play(self, item, index=None):
        self.plays.append((item, index))
        return True

    def is_playing(self):
        return self.playing

    def stop(self):
        self.playing = False

    def toggle_pause(self):
        self.toggles += 1

    def seek(self, seconds, mode="relative"):
        self.seeks.append(seconds)

    def set_volume(self, vol):
        self.volumes.append(vol)

    def toggle_mute(self):
        self.mutes += 1
        self.muted = not self.muted

    def tick(self):
        pass

    def get_position(self):
        return 0.0

    def get_duration(self):
        return 0.0

    def current_item(self):
        return None

    def is_auto_next(self):
        return True

    def set_auto_next(self, enabled):
        pass


def _mk_window():
    from app.ui.widgets.video_player_window import VideoPlayerWindow
    ctl = _FakeCtl()
    w = VideoPlayerWindow(ctl)
    return w, ctl


def _key(w, key):
    ev = QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
    w.keyPressEvent(ev)


class TestKeyboardShortcuts:
    def test_space_toggles_pause(self):
        """空格 → overlay 按钮全链（SVG 图标态切换 + toggle_pause 被调）。"""
        w, ctl = _mk_window()
        try:
            w.overlay._paused = False
            w.overlay.set_play_icon(False)
            _key(w, Qt.Key_Space)
            assert ctl.toggles == 1
            assert w.overlay._play_state == "pause"
            _key(w, Qt.Key_Space)
            assert ctl.toggles == 2
            assert w.overlay._play_state == "play"
        finally:
            w._timer.stop()
            w._hide_timer.stop()
            w._sidebar_hide_timer.stop()

    def test_left_right_seek(self):
        """←/→ → seek ±5。"""
        w, ctl = _mk_window()
        try:
            _key(w, Qt.Key_Left)
            _key(w, Qt.Key_Right)
            assert ctl.seeks == [-5, 5]
        finally:
            w._timer.stop()

    def test_up_down_volume(self):
        """↑/↓ → 音量 ±5（clamp 0-100；走滑块全链）。"""
        w, ctl = _mk_window()
        try:
            w.overlay.slider_vol.setValue(70)
            _key(w, Qt.Key_Up)
            assert ctl.volumes[-1] == 75
            _key(w, Qt.Key_Down)
            assert ctl.volumes[-1] == 70
            # clamp 下界
            w.overlay.slider_vol.setValue(2)
            _key(w, Qt.Key_Down)
            assert ctl.volumes[-1] == 0
        finally:
            w._timer.stop()

    def test_f_toggles_fullscreen(self, monkeypatch):
        """F → 全屏切换（showFullScreen/showNormal mock——offscreen 真全屏清理崩）。"""
        w, _ = _mk_window()
        try:
            calls = []
            monkeypatch.setattr(w, "showFullScreen", lambda: calls.append("fs"))
            monkeypatch.setattr(w, "showNormal", lambda: calls.append("n"))
            _key(w, Qt.Key_F)
            assert calls == ["fs"]
            w._fullscreen = True
            _key(w, Qt.Key_F)
            assert calls == ["fs", "n"]
        finally:
            w._timer.stop()

    def test_m_toggles_mute(self):
        """M → 静音切换（按钮全链：toggle_mute 被调 + 图标态翻转）。"""
        w, ctl = _mk_window()
        try:
            w.overlay._muted = False
            w.overlay.set_volume_icon()
            _key(w, Qt.Key_M)
            assert ctl.mutes == 1
            assert w.overlay._vol_state == "muted"
            _key(w, Qt.Key_M)
            assert ctl.mutes == 2
            assert w.overlay._vol_state == "high"
        finally:
            w._timer.stop()

    def test_esc_fullscreen_exits(self, monkeypatch):
        """Esc（全屏时）→ 退全屏（非关窗）。"""
        w, _ = _mk_window()
        try:
            def fake_normal():
                w._fullscreen = False
            monkeypatch.setattr(w, "showNormal", fake_normal)
            w._fullscreen = True
            w.close = lambda: None
            _key(w, Qt.Key_Escape)
            assert w._fullscreen is False
        finally:
            w._timer.stop()


class TestFocusManagement:
    def test_container_strong_focus(self):
        """容器 StrongFocus（点画面可收键盘焦点）。"""
        w, _ = _mk_window()
        try:
            assert w.container.focusPolicy() == Qt.StrongFocus
        finally:
            w._timer.stop()

    def test_container_click_gains_focus(self, monkeypatch):
        """点画面 → 容器收焦点（键盘事件到 Qt）。
        offscreen 无真实焦点机制——验证 setFocus 被调（焦点请求发出）。"""
        w, _ = _mk_window()
        try:
            focus_calls = []
            monkeypatch.setattr(w.container, "setFocus",
                                lambda: focus_calls.append(1))
            ev = QMouseEvent(QEvent.MouseButtonPress, QPoint(5, 5),
                             Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
            w.container.mousePressEvent(ev)
            assert focus_calls == [1]
        finally:
            w._timer.stop()

    def test_overlay_click_restores_focus(self, monkeypatch):
        """点 overlay 按钮 → 焦点归还容器（防点按钮后键盘失效）。
        offscreen 无真实焦点机制——验证容器 setFocus 被调。"""
        w, _ = _mk_window()
        try:
            focus_calls = []
            monkeypatch.setattr(w.container, "setFocus",
                                lambda: focus_calls.append(1))
            w.overlay.btn_toggle.click()
            assert focus_calls == [1]
            w.overlay.slider_pos.sliderReleased.emit()
            assert len(focus_calls) == 2
        finally:
            w._timer.stop()
