"""电影内嵌播放窗口（S0 骨架 + S1 最小内嵌 + S2 overlay 控制层）。

架构：Qt 顶层播放窗口 + VideoContainer（WA_NativeWindow，mpv --wid 渲染目标）
+ 独立 ControlOverlay 顶层窗口（Qt::Tool | StaysOnTop，天然浮在 mpv 之上——
z-order 原型实证：SetWindowPos 调兄弟 z-order 无效，独立顶层窗口是可靠解）。

职责边界（硬约束）：播放窗口只做"呈现 + 发控制指令"，业务全在 MovieController
（唯一控制器——搜索/互斥/连播/字幕/状态写入）。本文件零业务复制。

S2：overlay 填 B站式底部控制条——⏯ ⏹ 进度条(拖动预览/松手 seek) 时间
倍速菜单(9 档当前档高亮) 音量滑块/静音 全屏；焦点管理 WS_EX_NOACTIVATE
（点击控件不抢 mpv 焦点，mpv 空格暂停仍生效——S2 核心）；overlay 常显
（鼠标浮现/隐藏 S3 做）；宽度跟随窗口（resizeEvent 重定位 + 重设宽）。
"""
import ctypes

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMenu, QPushButton,
                               QSlider, QVBoxLayout, QWidget)

# 倍速档位（对齐 B站/爱奇艺标准 9 档；与 movie_panel 同源值）
_SPEED_STEPS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]

# overlay 固定高度（对齐 S1 控制条 52；禁依赖未 show 时 height()=480 撑成灰块）
_OVERLAY_H = 52

# 焦点管理（S5 启用）：点击不激活（WS_EX_NOACTIVATE）——点 overlay 按钮不抢 mpv 焦点
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _speed_label(rate) -> str:
    return f"{rate:g}x" if rate != int(rate) else f"{int(rate)}x"


class VideoContainer(QWidget):
    """mpv --wid 渲染目标：WA_NativeWindow 黑底容器（占满播放窗口）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setStyleSheet("background:#000;")
        self._win_id = None

    def native_id(self):
        """容器 HWND（uint32——win32 句柄 32 位，负值 mpv 拒收）。
        WA_NativeWindow 强制 native，首次调用即创建句柄并缓存。"""
        if self._win_id is None:
            self._win_id = int(self.winId()) & 0xFFFFFFFF
        return self._win_id

    def reset_native_id(self):
        self._win_id = None


class ControlOverlay(QWidget):
    """独立顶层控制层（Qt::Tool | Frameless | StaysOnTop）——B站式底部控制条。
    只发控制指令给 movie_controller（mpv IPC），零业务复制。
    S2 布局：⏯ 暂停/继续 · ⏹ 停止 · 进度条(拖动预览/松手 seek) · 时间 ·
    倍速(弹菜单 9 档当前档高亮) · 音量滑块 · 静音 · 全屏。"""

    def __init__(self, window):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._window = window
        self._movie = window._movie
        self.setAttribute(Qt.WA_NativeWindow)
        # S2 修复：撤 WA_ShowWithoutActivating（Qt 在 Windows 用 SW_SHOWNA 显示且永不激活，
        # 与 NOACTIVATE 组合导致工具窗口在桌面不显示——回到 S1 已验证显示路径）。
        # 焦点管理（NOACTIVATE）整体推到 S5；本批接受"点 overlay 会抢 mpv 焦点"。
        self.setFixedHeight(_OVERLAY_H)
        self.setStyleSheet("background:rgba(28,28,28,210); border:1px solid #555;"
                           " color:#eee; font-size:12px;")
        self._speed_idx = 2          # 1.0x 默认档
        self._paused = False
        self._muted = False
        self._dragging = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        # ⏯ 暂停/继续
        self.btn_toggle = QPushButton("⏸")
        self.btn_toggle.setFixedWidth(44)
        self.btn_toggle.setToolTip("暂停/继续")
        self.btn_toggle.clicked.connect(self._on_toggle)
        lay.addWidget(self.btn_toggle)

        # ⏹ 停止（停播 + 关窗）
        self.btn_stop = QPushButton("⏹")
        self.btn_stop.setFixedWidth(40)
        self.btn_stop.setToolTip("停止播放")
        self.btn_stop.clicked.connect(window.request_close)
        lay.addWidget(self.btn_stop)

        # 进度条（拖动预览，松手才 seek）
        self.slider_pos = QSlider(Qt.Horizontal)
        self.slider_pos.setRange(0, 1000)
        self.slider_pos.setFixedWidth(260)
        self.slider_pos.sliderPressed.connect(self._on_slider_pressed)
        self.slider_pos.sliderReleased.connect(self._on_slider_released)
        self.slider_pos.valueChanged.connect(self._on_slider_value)
        lay.addWidget(self.slider_pos, 1)

        # 时间
        self.lbl_time = QLabel("0:00 / 0:00")
        lay.addWidget(self.lbl_time)

        # 倍速（弹菜单 9 档）
        self.btn_speed = QPushButton("倍速 1x")
        self.btn_speed.setToolTip("倍速（9 档）")
        self.btn_speed.clicked.connect(self._on_speed)
        lay.addWidget(self.btn_speed)

        # 音量滑块
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(70)
        self.slider_vol.setFixedWidth(90)
        self.slider_vol.setToolTip("音量")
        self.slider_vol.valueChanged.connect(self._on_volume)
        lay.addWidget(self.slider_vol)

        # 静音
        self.btn_mute = QPushButton("🔊")
        self.btn_mute.setFixedWidth(36)
        self.btn_mute.setToolTip("静音")
        self.btn_mute.clicked.connect(self._on_mute)
        lay.addWidget(self.btn_mute)

        # 全屏（S2 只触发；浮现/隐藏 S3）
        self.btn_fullscreen = QPushButton("⛶")
        self.btn_fullscreen.setFixedWidth(36)
        self.btn_fullscreen.setToolTip("全屏")
        self.btn_fullscreen.clicked.connect(window.request_fullscreen)
        lay.addWidget(self.btn_fullscreen)

    # ---- 焦点管理（S5 启用；本批只修 ctypes bug，不主动调用） ----

    def apply_focus_no_activate(self) -> bool:
        """给 overlay 顶层窗口设 WS_EX_NOACTIVATE：点击控件不激活窗口、
        不抢 mpv 焦点（mpv 空格暂停仍生效）。S5 启用（当前 show 不再自动调）。
        win32 特有；offscreen/失败返回 False 并打日志（不静默——S2 修复）。"""
        try:
            user32 = ctypes.windll.user32
            # S2 修复：设 argtypes 防 64 位 HWND 被 c_int 截断（句柄错 → 静默失败）
            user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                 ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            hwnd = ctypes.c_void_p(int(self.winId()))
            ex = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
            if not ex:
                print(f"[Overlay] GetWindowLongPtrW 失败 hwnd={int(self.winId())}")
                return False
            new_ex = int(ex) | _WS_EX_NOACTIVATE
            if not user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, ctypes.c_void_p(new_ex)):
                print(f"[Overlay] SetWindowLongPtrW 失败 hwnd={int(self.winId())}")
                return False
            return True
        except Exception as e:
            print(f"[Overlay] apply_focus_no_activate 异常: {e}")
            return False

    def showEvent(self, event):
        # S2 修复：不再自动设 NOACTIVATE（撤出本批，S5 恢复）——普通显示路径
        super().showEvent(event)

    # ---- 控制转发（只发指令，不复制业务） ----

    def _on_toggle(self):
        self._paused = not self._paused
        self.btn_toggle.setText("▶" if self._paused else "⏸")
        try:
            self._movie.toggle_pause()
        except Exception:
            pass

    def _on_slider_pressed(self):
        self._dragging = True

    def _on_slider_released(self):
        self._dragging = False
        try:
            dur = self._movie.get_duration()
            if dur > 0:
                self._movie.seek(
                    self.slider_pos.value() / 1000.0 * dur, mode="absolute")
        except Exception:
            pass

    def _on_slider_value(self, value):
        """拖动中 → 只预览时间（不 seek）；轮询不覆盖（_dragging 守卫）。"""
        if not self._dragging:
            return
        try:
            dur = self._movie.get_duration()
            if dur > 0:
                self.lbl_time.setText(
                    f"{_fmt(value / 1000.0 * dur)} / {_fmt(dur)}")
        except Exception:
            pass

    def _on_speed(self):
        menu = self._build_speed_menu()
        menu.exec_(self.btn_speed.mapToGlobal(self.btn_speed.rect().bottomLeft()))

    def _build_speed_menu(self) -> QMenu:
        """倍速菜单：9 档 + 当前档勾选高亮（对齐 B站弹菜单交互）。"""
        menu = QMenu(self)
        for i, rate in enumerate(_SPEED_STEPS):
            label = _speed_label(rate)
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(i == self._speed_idx)   # 当前档高亮
            act.triggered.connect(
                lambda _checked=False, r=rate, idx=i: self._apply_speed(r, idx))
        return menu

    def _apply_speed(self, rate, idx=None):
        """应用倍速档位（菜单 action 与测试共用入口）。"""
        if idx is None:
            try:
                idx = _SPEED_STEPS.index(rate)
            except ValueError:
                idx = self._speed_idx
        self._speed_idx = idx
        self.btn_speed.setText(f"倍速 {_speed_label(rate)}")
        try:
            self._movie.set_speed(rate)
        except Exception:
            pass

    def _on_volume(self, value):
        try:
            self._movie.set_volume(value)
        except Exception:
            pass

    def _on_mute(self):
        self._muted = not self._muted
        self.btn_mute.setText("🔇" if self._muted else "🔊")
        try:
            self._movie.toggle_mute()
        except Exception:
            pass

    # ---- 轮询刷新（窗口 _tick 调；拖动中进度条不动） ----

    def refresh(self, pos: float, dur: float):
        if not self._dragging:
            if dur > 0:
                ratio = min(1.0, max(0.0, pos / dur))
                self.slider_pos.setValue(int(ratio * 1000))
                self.lbl_time.setText(f"{_fmt(pos)} / {_fmt(dur)}")
            else:
                self.lbl_time.setText(f"{_fmt(pos)} / 0:00")


class VideoPlayerWindow(QWidget):
    """内嵌播放窗口：容器嵌 mpv 画面 + overlay 控制层（独立顶层，不被盖）。
    关窗 = 停播 + 隐藏（不销毁，winId 保留——下次播放直接复用窗口/内嵌目标）。
    Esc：全屏时退全屏；非全屏时关窗（停播）。"""

    # 停播信号（close / 播完 / mpv 异常自愈失败时发，面板清 embed 状态）
    stopped = Signal()

    def __init__(self, controller, title="电影播放"):
        super().__init__(None)
        self._movie = controller
        self.setWindowTitle(title)
        self.resize(960, 540)
        self._fullscreen = False
        self._playing = False
        self._title_base = title

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.container = VideoContainer()
        lay.addWidget(self.container)

        # 独立顶层 overlay（S2 底部控制条）
        self.overlay = ControlOverlay(self)
        self.overlay.hide()

        # 进度/连播轮询（embed 模式唯一驱动——面板轮询让位，见 movie_panel）
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

    # ---- 播放入口 ----

    def play(self, item, index=None):
        """注入容器 winId → 控制器播放 → 接管轮询/overlay。"""
        self._movie.set_embed_wid(self.container.native_id())
        ok = self._movie.play(item, index=index)
        if ok:
            name = (item.get("vod_name") or "")[:20]
            self.setWindowTitle(f"{name} - {self._title_base}")
            self._playing = True
            self.overlay.show()
            self._timer.start()
            self._position_overlay()
        return ok

    def attach_current(self):
        """外部已开始播放（语音路径：controller.play 已用注入的 winId 起 mpv），
        窗口负责接管轮询/overlay 呈现。"""
        if not self._movie.is_playing():
            return
        self._playing = True
        self.overlay.show()
        self._timer.start()
        self._position_overlay()

    # ---- 窗口动作（overlay 按钮入口；只呈现，不复制业务） ----

    def request_close(self):
        self.close()

    def raise_topmost(self):
        """z-order 置顶（Windows API，只动 z-order 不碰前台锁）。

        P0 卡死修复删掉 Qt raise_() 后窗口被宿主盖住（有声音、看不到）——
        直接 SetWindowPos(HWND_TOPMOST) + SWP_NOACTIVATE：
        不激活（不抢焦点、不触发跨进程激活死锁）；异步调用由面板 singleShot 排队
        （宿主喘息后置顶）。失败打日志不静默；offscreen/非 win32 异常兜底。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # HWND 64 位——argtypes 必须 c_void_p（c_int 截断静默失败，之前踩过）
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_int
            HWND_TOPMOST = ctypes.c_void_p(-1)
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            hwnd = ctypes.c_void_p(int(self.winId()))
            ok = user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            if not ok:
                print(f"[播放窗口] SetWindowPos 置顶失败 hwnd={int(self.winId())}")
        except Exception as e:
            print(f"[播放窗口] SetWindowPos 置顶异常: {e}")

    def request_fullscreen(self):
        """全屏按钮：Qt 窗口全屏（mpv --wid 内嵌无独立窗口，set_fullscreen 无效）。
        全屏浮现在 S3。"""
        self.showFullScreen()

    def toggle_pause(self):
        try:
            if self._movie.is_playing():
                self._movie.toggle_pause()
        except Exception:
            pass

    # ---- 轮询（连播检测 + 进度/overlay 刷新；异常静默降级） ----

    def _tick(self):
        try:
            # 连播检测先行：播完一集 idle 上升沿 → 下一集（is_playing 恢复 True）。
            # 若先查 is_playing，播完瞬间会误判"停播"直接收尾，连播永远起不来。
            self._movie.tick()
            if not self._movie.is_playing():
                self._on_stopped()
                return
            pos = self._movie.get_position()
            dur = self._movie.get_duration()
            it = self._movie.current_item() or {}
            name = (it.get("vod_name") or "")[:20]
            if dur > 0:
                self.setWindowTitle(f"{name}  {_fmt(pos)}/{_fmt(dur)} - {self._title_base}")
            else:
                self.setWindowTitle(f"{name}  {_fmt(pos)}/0:00 - {self._title_base}")
            self.overlay.refresh(pos, dur)
        except Exception:
            pass

    def _on_stopped(self):
        """停播（播完/停止/异常）：停轮询 + 隐藏 overlay + 通知面板。"""
        self._playing = False
        self._timer.stop()
        self.overlay.hide()
        try:
            self.stopped.emit()
        except Exception:
            pass

    # ---- 窗口事件：关窗=停播（隐藏保留 winId）/ Esc ----

    def closeEvent(self, event):
        try:
            if self._playing or self._movie.is_playing():
                self._movie.stop()
        except Exception:
            pass
        self._on_stopped()
        self.setWindowTitle(self._title_base)
        self.hide()
        event.ignore()  # 不销毁窗口：winId 保留，下次播放直接复用

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self._fullscreen:
                self.showNormal()
            else:
                self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def showFullScreen(self):
        self._fullscreen = True
        super().showFullScreen()
        self._position_overlay()

    def showNormal(self):
        self._fullscreen = False
        super().showNormal()
        self._position_overlay()

    # ---- overlay 定位（跟随窗口 move/resize/全屏；宽度跟随容器） ----

    def _position_overlay(self):
        geo = self.geometry()
        # 宽度跟随窗口（留边），上限 1100（防超宽条）；高度固定 _OVERLAY_H——
        # S2 修复：禁依赖 self.overlay.height()（未 show 时 = QWidget 默认 480 → 480 灰块）
        w = max(480, min(geo.width() - 40, 1100))
        self.overlay.resize(w, _OVERLAY_H)
        x = geo.x() + (geo.width() - w) // 2
        y = geo.y() + geo.height() - _OVERLAY_H - 24
        self.overlay.move(x, y)
        self.overlay.raise_()

    def moveEvent(self, event):
        self._position_overlay()
        super().moveEvent(event)

    def resizeEvent(self, event):
        self._position_overlay()
        super().resizeEvent(event)
