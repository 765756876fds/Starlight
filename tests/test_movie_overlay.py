"""电影内嵌播放 S2：overlay 控制层测试（offscreen）。

覆盖：B站式底部控制条结构 / 进度条轮询+拖动预览+松手 seek / 播放暂停停止 /
倍速 9 档菜单（当前档高亮）/ 音量静音 / 全屏触发 / 焦点管理 WS_EX_NOACTIVATE /
overlay 定位跟随窗口 resize。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])


class _FakeCtl:
    """S2 测试用 mock 控制器（记录控制指令调用）。"""

    def __init__(self):
        self.seeks = []
        self.speeds = []
        self.volumes = []
        self.mutes = 0
        self.toggles = 0
        self.stopped = 0
        self.plays = []              # S6：play(item, index) 调用记录
        self.pos = 60.0
        self.dur = 120.0
        self.playing = True

    def set_embed_wid(self, wid):
        pass

    def play(self, item, index=None):
        self.plays.append((item, index))
        return True

    def is_playing(self):
        return self.playing

    def stop(self):
        self.stopped += 1
        self.playing = False

    def toggle_pause(self):
        self.toggles += 1

    def tick(self):
        pass

    def get_position(self):
        return self.pos

    def get_duration(self):
        return self.dur

    def current_item(self):
        return None

    def is_auto_next(self):
        return True

    def set_auto_next(self, enabled):
        pass

    def seek(self, seconds, mode="relative"):
        self.seeks.append((seconds, mode))

    def set_speed(self, rate):
        self.speeds.append(rate)

    def set_volume(self, vol):
        self.volumes.append(vol)

    def toggle_mute(self):
        self.mutes += 1


def _mk_window():
    from app.ui.widgets.video_player_window import VideoPlayerWindow
    ctl = _FakeCtl()
    w = VideoPlayerWindow(ctl)
    return w, ctl


class TestOverlayStructure:
    def test_controls_present(self):
        """S6 控制条控件齐全（B站布局）：播放/暂停 · 下一集 · 进度条 · 时间 ·
        音量 · 倍速 · 字幕 · 全屏；停止已取消（用户：停止=关窗）。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            assert ov.btn_toggle is not None
            assert ov.btn_next is not None
            assert ov.slider_pos is not None
            assert ov.lbl_time is not None
            assert ov.btn_speed is not None
            assert ov.slider_vol is not None
            assert ov.btn_mute is not None
            assert ov.btn_subtitle is not None
            assert ov.btn_fullscreen is not None
            assert not hasattr(ov, "btn_stop")          # 停止按钮已取消
            assert ov.windowFlags() & Qt.Tool
            assert ov.windowFlags() & Qt.FramelessWindowHint
            # S7：去永久置顶（owner = 播放窗口，被盖跟随）——不再全局 TOPMOST
            assert not (ov.windowFlags() & Qt.WindowStaysOnTopHint)
            assert ov.parent() is w                      # owner 关系（跟随播放窗口）
        finally:
            w._timer.stop()

    def test_speed_steps_nine(self):
        """倍速 9 档全在（B站/爱奇艺标准档）。"""
        from app.ui.widgets.video_player_window import _SPEED_STEPS
        assert _SPEED_STEPS == [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]


class TestProgress:
    def test_refresh_updates_slider_and_time(self):
        w, ctl = _mk_window()
        try:
            ov = w.overlay
            ctl.pos, ctl.dur = 30.0, 120.0
            ov.refresh(30.0, 120.0)
            assert ov.slider_pos.value() == 250        # 30/120 → 250/1000
            assert ov.lbl_time.text() == "0:30 / 2:00"
        finally:
            w._timer.stop()

    def test_refresh_no_duration(self):
        w, _ = _mk_window()
        try:
            ov = w.overlay
            ov.refresh(45.0, 0.0)
            assert ov.lbl_time.text() == "0:45 / 0:00"
        finally:
            w._timer.stop()

    def test_dragging_preview_does_not_seek(self):
        """拖动中只预览时间、不 seek（防拖拽中卡顿）。"""
        w, ctl = _mk_window()
        try:
            ov = w.overlay
            ov.slider_pos.setValue(500)
            ov.slider_pos.sliderPressed.emit()
            assert ov._dragging is True
            ov.slider_pos.setValue(750)                # 拖动中 valueChanged
            assert ctl.seeks == []                     # 未 seek
            assert ov.lbl_time.text() == "1:30 / 2:00"  # 预览时间
            ov.slider_pos.sliderReleased.emit()        # 松手 → seek
            assert ctl.seeks == [(90.0, "absolute")]   # 750/1000 * 120 = 90s
            assert ov._dragging is False
        finally:
            w._timer.stop()

    def test_dragging_blocks_refresh_overwrite(self):
        """拖动中轮询 refresh 不覆盖滑块（防拖动被打断）。"""
        w, ctl = _mk_window()
        try:
            ov = w.overlay
            ov.slider_pos.sliderPressed.emit()
            ov.slider_pos.setValue(400)
            ov.refresh(30.0, 120.0)                    # 轮询到了
            assert ov.slider_pos.value() == 400        # 未被拉回 250
            ov.slider_pos.sliderReleased.emit()
            ov.refresh(30.0, 120.0)
            assert ov.slider_pos.value() == 250
        finally:
            w._timer.stop()


class TestPlaybackControls:
    def test_toggle_pauses_and_switches_icon(self):
        w, ctl = _mk_window()
        try:
            ov = w.overlay
            assert ov._play_state == "play"              # S6：SVG 图标态
            ov.btn_toggle.click()
            assert ctl.toggles == 1
            assert ov._paused is True
            assert ov._play_state == "pause"
            ov.btn_toggle.click()
            assert ctl.toggles == 2
            assert ov._paused is False
            assert ov._play_state == "play"
        finally:
            w._timer.stop()

    def test_next_button_switches_episode(self):
        """下一集：play(item, index+1)；最后一集禁用 + 无操作。"""
        w, ctl = _mk_window()
        try:
            item = {"vod_name": "剧", "play_url":
                    "第1集$u1#第2集$u2#第3集$u3"}
            w.play(item, index=0)
            assert w.overlay.btn_next.isEnabled() is True
            w.overlay.btn_next.click()
            assert ctl.plays[-1] == (item, 1)            # index+1
            # 最后一集 → 禁用 + 无操作
            w.play(item, index=2)
            assert w.overlay.btn_next.isEnabled() is False
            n = len(ctl.plays)
            w.overlay.btn_next.click()
            assert len(ctl.plays) == n
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()


class TestSpeed:
    def test_apply_speed_sets_mpv_and_updates_button(self):
        w, ctl = _mk_window()
        try:
            ov = w.overlay
            ov._apply_speed(2.0)
            assert ctl.speeds == [2.0]
            assert ov.btn_speed.text() == "倍速 2x"
            assert ov._speed_idx == 6
        finally:
            w._timer.stop()

    def test_apply_speed_current_idx_highlight(self):
        w, _ = _mk_window()
        try:
            ov = w.overlay
            ov._apply_speed(0.75)
            assert ov._speed_idx == 1                  # 当前档（菜单勾选依据）
            ov._apply_speed(1.5)
            assert ov._speed_idx == 4
        finally:
            w._timer.stop()

    def test_speed_menu_builds_nine_checkable(self):
        """倍速菜单：9 项 + 当前档勾选（对齐 B站弹菜单交互）。"""
        w, _ = _mk_window()
        try:
            ov = w.overlay
            menu = ov._build_speed_menu()
            actions = menu.actions()
            assert len(actions) == 9
            checked = [a for a in actions if a.isChecked()]
            assert len(checked) == 1
            assert checked[0].text() == "1x"           # 默认 1.0x 高亮
        finally:
            w._timer.stop()


class TestVolumeMute:
    def test_volume_slider_sets_mpv(self):
        w, ctl = _mk_window()
        try:
            ov = w.overlay
            ov.slider_vol.setValue(42)
            assert ctl.volumes[-1] == 42
        finally:
            w._timer.stop()

    def test_mute_toggles_and_icon(self):
        w, ctl = _mk_window()
        try:
            ov = w.overlay
            assert ov._vol_state == "high"              # S6：SVG 三态图标
            ov.btn_mute.click()
            assert ctl.mutes == 1
            assert ov._muted is True
            assert ov._vol_state == "muted"
            ov.btn_mute.click()
            assert ctl.mutes == 2
            assert ov._muted is False
            assert ov._vol_state == "high"
            # 低音量 → volume_low 态
            ov.slider_vol.setValue(5)
            assert ov._vol_state == "low"
        finally:
            w._timer.stop()


class TestFullscreen:
    def test_fullscreen_button_triggers_window_fullscreen(self):
        w, _ = _mk_window()
        try:
            w.show()
            w.overlay.btn_fullscreen.click()
            assert w._fullscreen is True
            assert w.isFullScreen() is True
            w.showNormal()
        finally:
            w._timer.stop()


class TestFixedHeightAndFlags:
    def test_overlay_fixed_height(self):
        """overlay 固定高 _OVERLAY_H（S7 两行布局 = 72；未 show 即 72；不随窗口
        resize 变——S2 修复防 480 灰块）。"""
        from app.ui.widgets.video_player_window import _OVERLAY_H
        w, _ = _mk_window()
        try:
            assert w.overlay.height() == _OVERLAY_H
            w.show()
            w.resize(1200, 800)
            w._position_overlay()
            assert w.overlay.height() == _OVERLAY_H      # 定位后仍 _OVERLAY_H
            w.resize(700, 400)
            w._position_overlay()
            assert w.overlay.height() == _OVERLAY_H
        finally:
            w._timer.stop()

    def test_no_wa_show_without_activating(self):
        """S2 修复：撤 WA_ShowWithoutActivating（SW_SHOWNA 不激活 + NOACTIVATE
        组合导致桌面不显示——回到 S1 已验证显示路径）。"""
        from PySide6.QtCore import Qt
        w, _ = _mk_window()
        try:
            assert w.overlay.testAttribute(Qt.WA_ShowWithoutActivating) is False
        finally:
            w._timer.stop()


class TestOverlayPosition:
    def test_overlay_follows_window_resize(self):
        """窗口 resize → overlay 重定位到底部居中 + 宽度跟随。"""
        from app.ui.widgets.video_player_window import _OVERLAY_H
        w, _ = _mk_window()
        w.show()
        try:
            w.resize(1000, 600)
            w._position_overlay()
            ov = w.overlay
            geo, ovg = w.geometry(), ov.geometry()
            assert ovg.width() == min(geo.width() - 40, 1100)  # 宽度跟随（留边）
            assert ovg.height() == _OVERLAY_H            # 固定高（S7 两行）
            # 底部居中：overlay 底缘贴窗口底缘（± 边距容差）
            assert abs((ovg.y() + ovg.height()) - (geo.y() + geo.height() - 24)) <= 2
            center_x = ovg.x() + ovg.width() / 2
            win_center_x = geo.x() + geo.width() / 2
            assert abs(center_x - win_center_x) <= 2
        finally:
            w._timer.stop()

    def test_overlay_width_cap(self):
        """超宽窗口 → overlay 宽度封顶 1100。"""
        w, _ = _mk_window()
        w.show()
        try:
            w.resize(2400, 800)
            w._position_overlay()
            assert w.overlay.width() == 1100
        finally:
            w._timer.stop()


class TestOverlayShowHide:
    """S3：全屏进出 + 鼠标浮现/隐藏（B站式）——窗口级鼠标跟踪 + 3s 静止隐藏定时器。"""

    @staticmethod
    def _mk_mouse_move(pos):
        from PySide6.QtCore import QEvent, QPoint
        from PySide6.QtGui import QMouseEvent
        return QMouseEvent(QEvent.MouseMove, QPoint(*pos), Qt.NoButton,
                           Qt.NoButton, Qt.NoModifier)

    def test_mouse_move_shows_overlay_and_starts_timer(self):
        """鼠标移入窗口移动 → overlay 浮现 + 3s 隐藏定时器启动。"""
        w, _ = _mk_window()
        try:
            w.overlay.hide()
            w.mouseMoveEvent(self._mk_mouse_move((100, 100)))
            assert w.overlay.isVisible() is True
            assert w._hide_timer.isActive() is True
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_hide_timeout_hides_overlay(self):
        """3s 静止超时 → 控制条隐藏（直接触发超时回调模拟）。"""
        w, _ = _mk_window()
        try:
            w._show_overlay()
            assert w.overlay.isVisible() is True
            w._on_hide_timeout()
            assert w.overlay.isVisible() is False
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_hover_prevents_hide(self):
        """鼠标 hover overlay（操作中）→ 停计时 + 超时不隐藏。"""
        w, _ = _mk_window()
        try:
            w._show_overlay()
            w._on_overlay_hover(True)
            assert w._hide_timer.isActive() is False
            w._on_hide_timeout()
            assert w.overlay.isVisible() is True
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_leave_event_hides_overlay(self):
        """鼠标移出窗口 → 立即隐藏 + 停计时（overlay 已远离鼠标位置）。"""
        from PySide6.QtCore import QEvent
        w, _ = _mk_window()
        try:
            w._show_overlay()
            w.overlay.move(9999, 9999)          # overlay 远离真实鼠标位置
            w.leaveEvent(QEvent(QEvent.Leave))
            assert w.overlay.isVisible() is False
            assert w._hide_timer.isActive() is False
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_non_fullscreen_show_hide_works(self):
        """非全屏窗口模式同样生效：隐藏时控制条 + 指针一起隐（S3 修），浮现恢复。"""
        w, _ = _mk_window()
        try:
            assert w._fullscreen is False
            w._show_overlay()
            assert w.overlay.isVisible() is True
            w._on_hide_timeout()
            assert w.overlay.isVisible() is False
            assert w.cursor().shape() == Qt.BlankCursor   # 窗口模式也隐指针（S3 修）
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_hide_sets_blank_cursor_window_mode(self):
        """S3 修：窗口模式静止超时 → 指针也隐藏（B站：控制条 + 指针同步隐）。"""
        w, _ = _mk_window()
        try:
            w._fullscreen = False
            w._show_overlay()
            w._on_hide_timeout()
            assert w.cursor().shape() == Qt.BlankCursor
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_passthrough_sets_transparent_on_mpv_child(self, monkeypatch):
        """S3 穿透：EnumChildWindows 找到 mpv 子窗口 → SetWindowLongPtrW
        设 WS_EX_TRANSPARENT（exstyle | 0x20）。"""
        import ctypes
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        try:
            user32 = ctypes.windll.user32
            enum_hwnds = []
            set_calls = []

            def fake_enum(hwnd, cb, _):
                enum_hwnds.append(hwnd)
                cb(ctypes.c_void_p(0x1234), None)   # 模拟枚举到一个 mpv 子窗口
                return 1

            monkeypatch.setattr(user32, "EnumChildWindows", fake_enum)
            monkeypatch.setattr(
                user32, "GetClassNameW",
                lambda h, buf, n: (setattr(buf, "value", "mpv"), 3)[1])
            monkeypatch.setattr(
                user32, "GetWindowLongPtrW",
                lambda h, idx: ctypes.c_void_p(0x4))
            monkeypatch.setattr(
                user32, "SetWindowLongPtrW",
                lambda h, idx, v: set_calls.append((h, idx, v)))
            w._apply_mpv_mouse_passthrough()
            assert len(enum_hwnds) == 1                       # 枚举容器子窗口
            assert len(set_calls) == 1                        # 命中 mpv 子窗口
            assert set_calls[0][1] == -20                     # GWL_EXSTYLE
            assert set_calls[0][2].value == 0x4 | 0x20        # ex | WS_EX_TRANSPARENT
        finally:
            w._timer.stop()

    def test_passthrough_argtypes_and_no_child_graceful(self, monkeypatch):
        """穿透：argtypes 全 c_void_p（64 位防截断）；无 mpv 子窗口 → 打日志不崩。"""
        import ctypes
        from app.ui.widgets.video_player_window import VideoPlayerWindow
        ctl = _FakeCtl()
        w = VideoPlayerWindow(ctl)
        try:
            user32 = ctypes.windll.user32
            monkeypatch.setattr(user32, "EnumChildWindows",
                                lambda hwnd, cb, _: 0)        # 无子窗口
            w._apply_mpv_mouse_passthrough()                  # 不抛异常
            assert user32.EnumChildWindows.argtypes == [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            assert user32.GetWindowLongPtrW.argtypes == [
                ctypes.c_void_p, ctypes.c_int]
            assert user32.SetWindowLongPtrW.argtypes == [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        finally:
            w._timer.stop()

    def test_fullscreen_hide_sets_blank_cursor(self):
        """全屏沉浸：隐藏时指针也隐藏（BlankCursor）；浮现恢复（ArrowCursor）。"""
        w, _ = _mk_window()
        try:
            w._fullscreen = True                 # 直接置标志（offscreen 不真全屏）
            w._show_overlay()
            w._on_hide_timeout()
            assert w.cursor().shape() == Qt.BlankCursor
            w._show_overlay()
            assert w.cursor().shape() == Qt.ArrowCursor
        finally:
            w._fullscreen = False
            w._timer.stop()
            w._hide_timer.stop()

    def test_overlay_mouse_move_notifies_window(self):
        """overlay（独立顶层，鼠标在其上时播放窗口收不到事件）活动 →
        通知窗口浮现 + 重置隐藏计时。"""
        w, _ = _mk_window()
        try:
            w.overlay.hide()
            w._hide_timer.stop()
            w.overlay.mouseMoveEvent(self._mk_mouse_move((5, 5)))
            assert w.overlay.isVisible() is True
            assert w._hide_timer.isActive() is True
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_overlay_enter_leave_toggles_hover(self):
        """overlay enter → hover=True（停计时）；leave → hover=False。"""
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QEnterEvent
        w, _ = _mk_window()
        try:
            w._show_overlay()
            enter = QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5))
            w.overlay.enterEvent(enter)
            assert w._overlay_hover is True
            assert w._hide_timer.isActive() is False
            w.overlay.leaveEvent(QEvent(QEvent.Leave))
            assert w._overlay_hover is False
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_container_mouse_tracking_enabled(self):
        """容器开 mouseTracking：画面区无按键移动也收 mouseMoveEvent（穿透后事件到容器）。"""
        w, _ = _mk_window()
        try:
            assert w.container.hasMouseTracking() is True
        finally:
            w._timer.stop()

    def test_container_mouse_move_forwards_to_window(self):
        """容器 mouseMove（穿透后画面区事件）→ 转发窗口公开接口 → overlay 浮现 +
        重置 3s 隐藏计时；容器自身 cursor 复原。"""
        w, _ = _mk_window()
        try:
            w.overlay.hide()
            w._hide_timer.stop()
            w.setCursor(Qt.BlankCursor)            # 隐藏态（窗口指针隐）
            w.container.mouseMoveEvent(self._mk_mouse_move((50, 50)))
            assert w.overlay.isVisible() is True
            assert w._hide_timer.isActive() is True
            assert w.container.cursor().shape() == Qt.ArrowCursor  # 容器复原
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_container_enter_forwards_to_window(self):
        """容器 enter（鼠标进入画面区）→ 转发窗口 → overlay 浮现。"""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QEnterEvent
        w, _ = _mk_window()
        try:
            w.overlay.hide()
            w._hide_timer.stop()
            w.container.enterEvent(
                QEnterEvent(QPointF(50, 50), QPointF(50, 50), QPointF(50, 50)))
            assert w.overlay.isVisible() is True
            assert w._hide_timer.isActive() is True
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_play_shows_overlay_and_starts_timer(self):
        """播放入口（attach_current）→ 浮现 + 启动隐藏计时（B站：开播短暂浮现）。"""
        w, _ = _mk_window()
        try:
            w.attach_current()
            assert w.overlay.isVisible() is True
            assert w._hide_timer.isActive() is True
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_fullscreen_button_enters_fullscreen(self, monkeypatch):
        """全屏按钮 → showFullScreen（_fullscreen 标志置位；mock 避免 offscreen
        真全屏切换导致 Qt 清理崩溃——功能断言等价）。"""
        from app.ui.widgets import video_player_window as vp
        monkeypatch.setattr(vp.VideoPlayerWindow, "showFullScreen",
                            lambda self: setattr(self, "_fullscreen", True))
        w, _ = _mk_window()
        try:
            w.overlay.btn_fullscreen.click()
            assert w._fullscreen is True
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_esc_exits_fullscreen(self, monkeypatch):
        """Esc：全屏时退全屏（keyPressEvent 分支；非全屏关窗 S1 已测）。"""
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from app.ui.widgets import video_player_window as vp
        monkeypatch.setattr(vp.VideoPlayerWindow, "showNormal",
                            lambda self: setattr(self, "_fullscreen", False))
        w, _ = _mk_window()
        try:
            w._fullscreen = True
            ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
            w.keyPressEvent(ev)
            assert w._fullscreen is False
        finally:
            w._timer.stop()
            w._hide_timer.stop()

    def test_stop_clears_hide_state(self):
        """停播 → 停隐藏计时 + 清 hover + 隐藏 overlay + 恢复指针。"""
        w, _ = _mk_window()
        try:
            w._fullscreen = True
            w._show_overlay()
            w._on_overlay_hover(True)
            w._on_stopped()
            assert w._playing is False
            assert w._overlay_hover is False
            assert w.overlay.isVisible() is False
            assert w._hide_timer.isActive() is False
        finally:
            w._timer.stop()
            w._hide_timer.stop()
