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
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

# 倍速档位（对齐 B站/爱奇艺标准 9 档；与 movie_panel 同源值）
_SPEED_STEPS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]

# overlay 固定高度（对齐 S1 控制条 52；禁依赖未 show 时 height()=480 撑成灰块）
_OVERLAY_H = 52

# S3：鼠标静止 3s 隐藏控制条（B站式观影沉浸；hover 控制条不隐藏）
_OVERLAY_HIDE_DELAY_MS = 3000

# S4：选集侧栏——右缘触发宽度 / 侧栏宽 / 静止 3s 隐藏
_SIDEBAR_EDGE_PX = 60
_SIDEBAR_W = 150
_SIDEBAR_HIDE_DELAY_MS = 3000

# 焦点管理（S5 启用）：点击不激活（WS_EX_NOACTIVATE）——点 overlay 按钮不抢 mpv 焦点
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _speed_label(rate) -> str:
    return f"{rate:g}x" if rate != int(rate) else f"{int(rate)}x"


class VideoContainer(QWidget):
    """mpv --wid 渲染目标：WA_NativeWindow 黑底容器（占满播放窗口）。

    S3 事件黑洞修复：容器占满客户区 → 穿透后画面区鼠标事件到容器（Qt 鼠标
    事件不冒泡，窗口收不到）→ 开 mouseTracking + 转发给窗口公开接口
    notify_mouse_activity()（浮现 + 重置 3s 隐藏计时）；cursor 跟随窗口
    （未显式设置则继承），移动时自身 unsetCursor 复原（防继承差异）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setStyleSheet("background:#000;")
        self._win_id = None
        self._window = parent              # 播放窗口（转发鼠标活动）
        self.setMouseTracking(True)        # 无按键移动也收 mouseMoveEvent
        # S5：点击画面 → 焦点到容器（--wid 嵌入后键盘焦点在 Qt 侧；容器聚焦
        # 后按键沿父链冒泡到播放窗口 keyPressEvent —— 键盘控制可用）
        self.setFocusPolicy(Qt.StrongFocus)

    def mousePressEvent(self, event):
        # S5：点画面收焦点（键盘事件到 Qt，不发 mpv——mpv 收不到 --wid 子窗口键盘）
        self.setFocus()
        super().mousePressEvent(event)

    def native_id(self):
        """容器 HWND（uint32——win32 句柄 32 位，负值 mpv 拒收）。
        WA_NativeWindow 强制 native，首次调用即创建句柄并缓存。"""
        if self._win_id is None:
            self._win_id = int(self.winId()) & 0xFFFFFFFF
        return self._win_id

    def reset_native_id(self):
        self._win_id = None

    def mouseMoveEvent(self, event):
        # 画面区鼠标活动（穿透后事件直达容器）→ 浮现 + 重置隐藏计时 + 右缘侧栏
        if self._window is not None:
            self._window.notify_mouse_activity()
            try:
                self._window.notify_mouse_position(event.position().x())
            except Exception:
                pass
        self.unsetCursor()                 # 容器 cursor 复原（防继承差异）
        super().mouseMoveEvent(event)

    def enterEvent(self, event):
        # 鼠标进入画面区 → 浮现（移入立即显示，不启动立即隐藏计时）
        if self._window is not None:
            self._window.notify_mouse_activity()
        self.unsetCursor()
        super().enterEvent(event)


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
        # S3 鼠标浮现/隐藏：overlay 是独立顶层窗口——鼠标在 overlay 上时播放窗口
        # 收不到 mouseMoveEvent，overlay 自行跟踪并通知窗口（防操作中消失）。
        self.setMouseTracking(True)

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

        # S5：任何控件操作后焦点归还容器——防"点了按钮后键盘失效"
        # （--wid 嵌入键盘在 Qt 侧；overlay 独立顶层点击不天然给容器焦点，
        # 不用 NOACTIVATE——S2 教训：NOACTIVATE 组合桌面不显示）
        self._restore_focus = self._make_restore_focus(window)
        for w in (self.btn_toggle, self.btn_stop, self.btn_speed,
                  self.btn_mute, self.btn_fullscreen):
            w.clicked.connect(self._restore_focus)
        self.slider_pos.sliderReleased.connect(self._restore_focus)
        self.slider_vol.sliderReleased.connect(self._restore_focus)

    @staticmethod
    def _make_restore_focus(window):
        """返回"焦点还给容器"的调用（防跨类私有依赖；失败静默）。"""
        def _restore():
            try:
                window.container.setFocus()
            except Exception:
                pass
        return _restore

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

    # ---- S3：overlay 鼠标跟踪（独立顶层窗口，鼠标在其上时播放窗口收不到事件） ----

    def enterEvent(self, event):
        # 鼠标进入控制条 → 通知窗口"hover 中"（停止隐藏计时，防操作中消失）
        try:
            self._window._on_overlay_hover(True)
        except Exception:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event):
        try:
            self._window._on_overlay_hover(False)
        except Exception:
            pass
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        # overlay 上活动 = 仍在操作 → 通知窗口浮现 + 重置隐藏计时
        try:
            self._window._show_overlay()
        except Exception:
            pass
        super().mouseMoveEvent(event)

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


class EpisodeSidebar(QWidget):
    """右侧选集侧栏（S4，对齐 B站）：独立顶层（同 overlay 模式——
    Qt::Tool | Frameless | StaysOnTop，原型验证 z-order 可行不被 mpv 盖）。

    贴播放窗口右缘纵向列出分集（第1集/第2集…，QListWidget 自带滚动——
    剧集几十集可滚）；当前集高亮；点击切集只发指令（复用窗口 play 链）。
    单集（电影）不显示。浮现/隐藏由窗口侧控制（右缘鼠标触发 + 3s 静止隐藏 +
    hover 不隐藏）——本类只渲染/交互/通知 hover 状态。
    """

    def __init__(self, window):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._window = window
        self.setAttribute(Qt.WA_NativeWindow)
        self.setFixedWidth(_SIDEBAR_W)
        self.setStyleSheet(
            "background:rgba(24,24,24,225); border:1px solid #555;"
            " color:#eee; font-size:12px;")
        self._episodes = []          # [(name, url), ...]
        self._current = 0

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget{background:transparent;border:none;}"
            "QListWidget::item{padding:6px 10px;}"
            "QListWidget::item:selected{background:#3a6ea5;color:#fff;}")
        self._list.itemClicked.connect(self._on_click)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._list)

        # hover 跟踪（独立顶层窗口：鼠标在其上时播放窗口收不到事件）
        self.setMouseTracking(True)

    # ---- 数据 ----

    def episode_count(self) -> int:
        return len(self._episodes)

    def set_episodes(self, episodes, current=0):
        """episodes: [(name, url), ...]（split_play_url 结果）。<=1 集 → 隐藏
        （单集电影无选集）；否则重建列表 + 当前集高亮。不自动 show（由鼠标
        右缘触发），隐藏状态由窗口 manage。"""
        self._episodes = list(episodes or [])
        self._current = max(0, int(current or 0))
        self._rebuild()
        if len(self._episodes) <= 1:
            self.hide()

    def _rebuild(self):
        self._list.clear()
        for i in range(len(self._episodes)):
            item = QListWidgetItem(f"第{i + 1}集")
            item.setData(Qt.UserRole, i)
            if i == self._current:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self._list.addItem(item)
        if self._episodes:
            self._list.setCurrentRow(self._current)   # 当前集高亮（选中态）

    def set_current(self, index):
        """切集后高亮更新（不重建列表）。"""
        self._current = max(0, int(index or 0))
        if self._episodes:
            self._list.setCurrentRow(self._current)

    # ---- 交互 ----

    def _on_click(self, item):
        try:
            idx = int(item.data(Qt.UserRole))
            self._window.switch_episode(idx)   # 只发指令（复用窗口 play 链）
        except Exception as e:
            print(f"[选集侧栏] 切集异常: {e}")

    # ---- hover 通知（独立顶层：播放窗口收不到侧栏上的鼠标事件） ----

    def enterEvent(self, event):
        try:
            self._window._on_sidebar_hover(True)
        except Exception:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event):
        try:
            self._window._on_sidebar_hover(False)
        except Exception:
            pass
        super().leaveEvent(event)


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
        self._current_item = None        # S4：当前播放 item（切集复用）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.container = VideoContainer(self)
        lay.addWidget(self.container)

        # 独立顶层 overlay（S2 底部控制条）
        self.overlay = ControlOverlay(self)
        self.overlay.hide()

        # S4：右侧选集侧栏（独立顶层；单集隐藏；右缘鼠标触发 + 3s 静止隐藏）
        self.sidebar = EpisodeSidebar(self)
        self.sidebar.hide()
        self._sidebar_hover = False
        self._sidebar_hide_timer = QTimer(self)
        self._sidebar_hide_timer.setSingleShot(True)
        self._sidebar_hide_timer.timeout.connect(self._on_sidebar_hide_timeout)

        # 进度/连播轮询（embed 模式唯一驱动——面板轮询让位，见 movie_panel）
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

        # S3 鼠标浮现/隐藏：窗口级鼠标跟踪 + 3s 静止隐藏定时器（B站式）
        self.setMouseTracking(True)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hide_timeout)
        self._overlay_hover = False      # 鼠标在 overlay 上（操作中不隐藏）
        # S3 穿透（--wid 路线确认）：mpv 子窗口设 WS_EX_TRANSPARENT 鼠标穿透 →
        # 画面区域事件直达 Qt（不用轮询兜底）。play/attach 时 _apply_mpv_mouse_passthrough。

    # ---- 播放入口 ----

    def play(self, item, index=None):
        """注入容器 winId → 控制器播放 → 接管轮询/overlay/侧栏。"""
        self._movie.set_embed_wid(self.container.native_id())
        ok = self._movie.play(item, index=index)
        if ok:
            name = (item.get("vod_name") or "")[:20]
            self.setWindowTitle(f"{name} - {self._title_base}")
            self._playing = True
            self._current_item = item           # S4：切集复用
            # S4：剧集列表（split_play_url 拆解；单集 → 侧栏隐藏）
            try:
                from app.movie.movie_controller import split_play_url
                eps = split_play_url(item.get("play_url") or "")
            except Exception:
                eps = []
            self.sidebar.set_episodes(eps, index or 0)
            self._apply_mpv_mouse_passthrough()   # S3：mpv 子窗口鼠标穿透（幂等）
            self._show_overlay()          # S3：浮现 + 启动 3s 隐藏计时
            self._timer.start()
            self._position_overlay()
        return ok

    def attach_current(self):
        """外部已开始播放（语音路径：controller.play 已用注入的 winId 起 mpv），
        窗口负责接管轮询/overlay 呈现。"""
        if not self._movie.is_playing():
            return
        self._playing = True
        self._apply_mpv_mouse_passthrough()   # S3：mpv 子窗口鼠标穿透（幂等）
        self._show_overlay()              # S3：浮现 + 启动 3s 隐藏计时
        self._timer.start()
        self._position_overlay()

    # ---- 窗口动作（overlay 按钮入口；只呈现，不复制业务） ----

    def request_close(self):
        self.close()

    def raise_topmost(self):
        """z-order 置顶（Windows API，只动 z-order 不碰前台锁）。

        P0 卡死修复删掉 Qt raise_() 后窗口被宿主盖住（有声音、看不到）——
        原 SetWindowPos(HWND_TOPMOST) + SWP_NOACTIVATE 置顶。实机发现
        HWND_TOPMOST = 全局永久置顶（Alt+Tab 切走仍被盖，压着一切软件）→
        改 HWND_TOP（同级置顶）：显示时浮在宿主主窗口之上、可正常切走。
        函数名保留 raise_topmost（调用点不动；实际语义 = 同级置顶）。
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
            HWND_TOP = ctypes.c_void_p(0)      # 同级置顶（非 TOPMOST 全局永久）
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            hwnd = ctypes.c_void_p(int(self.winId()))
            ok = user32.SetWindowPos(
                hwnd, HWND_TOP, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            if not ok:
                print(f"[播放窗口] SetWindowPos 置顶失败 hwnd={int(self.winId())}")
        except Exception as e:
            print(f"[播放窗口] SetWindowPos 置顶异常: {e}")

    def _apply_mpv_mouse_passthrough(self):
        """mpv 子窗口鼠标穿透（S3 实锤：--wid 子窗口吃鼠标事件 → WS_EX_TRANSPARENT）。

        EnumChildWindows 找容器下 mpv 子窗口（类名 mpv）→ SetWindowLongPtrW
        设 WS_EX_TRANSPARENT（0x20）→ Windows 命中测试跳过该子窗口 → 画面区域
        鼠标事件直达 Qt（悬浮控制条/hover/全屏恢复纯事件驱动，无轮询兜底）。
        幂等（连播/重播重设，保险）；失败打日志不崩；offscreen/非 win32 兜底。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # 64 位 HWND——argtypes 必须 c_void_p（c_int 截断静默失败，之前踩过）
            user32.EnumChildWindows.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            user32.EnumChildWindows.restype = ctypes.c_int
            user32.GetClassNameW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            user32.GetClassNameW.restype = ctypes.c_int
            user32.GetWindowLongPtrW.argtypes = [
                ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p

            WS_EX_TRANSPARENT = 0x20
            GWL_EXSTYLE = -20
            children = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def _enum_cb(hwnd, _):
                buf = ctypes.create_unicode_buffer(256)
                n = user32.GetClassNameW(hwnd, buf, 256)
                if n and buf.value == "mpv":
                    children.append(hwnd)
                return True

            user32.EnumChildWindows(
                ctypes.c_void_p(int(self.container.winId())),
                _enum_cb, None)
            if not children:
                print("[播放窗口] 未找到 mpv 子窗口（穿透未设——鼠标事件可能被吃）")
                return
            for hwnd in children:
                ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                # c_void_p 无 __int__（int() 会当 bytes 解析崩溃）→ 必须用 .value
                if isinstance(ex, int):
                    ex_int = ex
                else:
                    ex_val = getattr(ex, "value", None)
                    ex_int = int(ex_val) if ex_val is not None else 0
                user32.SetWindowLongPtrW(
                    hwnd, GWL_EXSTYLE, ctypes.c_void_p(ex_int | WS_EX_TRANSPARENT))
        except Exception as e:
            print(f"[播放窗口] mpv 鼠标穿透异常: {e}")

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
        """停播（播完/停止/异常）：停轮询 + 隐藏 overlay/侧栏 + 通知面板。"""
        self._playing = False
        self._timer.stop()
        self._hide_timer.stop()
        self._overlay_hover = False
        self.overlay.hide()
        self.unsetCursor()
        self._current_item = None
        self._sidebar_hide_timer.stop()
        self._sidebar_hover = False
        self.sidebar.hide()
        try:
            self.stopped.emit()
        except Exception:
            pass

    # ---- S3：鼠标浮现/隐藏（B站式：移入浮现，静止 3s 隐藏，hover 不隐藏） ----
    # 画面区域鼠标事件直达 Qt（mpv 子窗口 WS_EX_TRANSPARENT 穿透）→ 纯事件驱动。

    def _show_overlay(self):
        """鼠标活动 → 浮现控制条 + 重置隐藏定时器；指针恢复（全屏/窗口模式统一）。"""
        self.overlay.show()
        self.unsetCursor()
        self._hide_timer.start(_OVERLAY_HIDE_DELAY_MS)

    def notify_mouse_activity(self):
        """公开接口：鼠标活动（画面区容器/窗口/overlay）→ 浮现 + 重置 3s 隐藏计时。
        容器（VideoContainer）事件转发入口——跨类只走公开方法，不碰私有。"""
        self._show_overlay()

    # ---- S4：选集侧栏（右缘触发 / 3s 静止隐藏 / hover 不隐藏 / 切集） ----

    def notify_mouse_position(self, x):
        """公开接口：画面区鼠标 x 坐标（容器事件转发）——右缘区域 → 侧栏浮现。
        单集或未播放不触发。"""
        if not self._playing or self.sidebar.episode_count() <= 1:
            return
        if x >= self.width() - _SIDEBAR_EDGE_PX:
            self._show_sidebar()

    def switch_episode(self, index):
        """点集数 → 复用 play 链切集（穿透/overlay/title/高亮全走 play）。"""
        if self._current_item is None:
            return
        self.play(self._current_item, index=index)

    def _show_sidebar(self):
        """右缘鼠标 → 侧栏滑出 + 重置 3s 隐藏计时。"""
        if not self._playing or self.sidebar.episode_count() <= 1:
            return
        self._position_sidebar()
        self.sidebar.show()
        self._sidebar_hide_timer.start(_SIDEBAR_HIDE_DELAY_MS)

    def _on_sidebar_hide_timeout(self):
        """3s 无活动（或移出右缘后）→ 隐藏侧栏；hover（正在操作）不隐藏。"""
        if self._sidebar_hover:
            return
        self.sidebar.hide()

    def _on_sidebar_hover(self, hovering):
        """侧栏 hover 状态（独立顶层，播放窗口收不到其上的鼠标事件）：
        hover 中停计时；离开后重新计时。"""
        self._sidebar_hover = hovering
        if hovering:
            self._sidebar_hide_timer.stop()
        elif self.sidebar.isVisible():
            self._sidebar_hide_timer.start(_SIDEBAR_HIDE_DELAY_MS)

    def _position_sidebar(self):
        """贴播放窗口右缘（底部让开 overlay 控制条）。"""
        geo = self.geometry()
        h = max(100, geo.height() - _OVERLAY_H - 24)
        self.sidebar.resize(_SIDEBAR_W, h)
        self.sidebar.move(geo.x() + geo.width() - _SIDEBAR_W, geo.y())
        self.sidebar.raise_()

    def _on_hide_timeout(self):
        """3s 静止 → 隐藏控制条 + 指针（B站：指针与控制条同步隐）；
        hover（正在操作）不隐藏。"""
        if self._overlay_hover:
            return
        self.overlay.hide()
        self.setCursor(Qt.BlankCursor)      # 窗口模式也隐指针（S3 修）

    def _on_overlay_hover(self, hovering):
        """overlay hover 状态（控制条上操作防消失）：hover 中停计时；离开后
        鼠标若仍在窗口内 → 重新计时。"""
        self._overlay_hover = hovering
        if hovering:
            self._hide_timer.stop()
        elif self.overlay.isVisible():
            try:
                if self.geometry().contains(QCursor.pos()):
                    self._hide_timer.start(_OVERLAY_HIDE_DELAY_MS)
            except Exception:
                pass

    def mouseMoveEvent(self, event):
        self._show_overlay()
        try:
            self.notify_mouse_position(event.position().x())   # S4：右缘 → 侧栏
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def enterEvent(self, event):
        # 移入窗口 → 浮现 + 重置计时（不立即隐藏）
        self._show_overlay()
        super().enterEvent(event)

    def leaveEvent(self, event):
        # 移出窗口：鼠标在 overlay 上（hover）→ 停计时保持；否则立即隐藏
        self._hide_timer.stop()
        if self.overlay.isVisible():
            try:
                if self.overlay.geometry().contains(QCursor.pos()):
                    self._overlay_hover = True
                else:
                    self.overlay.hide()
                    if self._fullscreen:
                        self.setCursor(Qt.BlankCursor)
            except Exception:
                self.overlay.hide()
        super().leaveEvent(event)

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
        key = event.key()
        # Esc：全屏退全屏 / 非全屏关窗（已有）
        if key == Qt.Key_Escape:
            if self._fullscreen:
                self.showNormal()
            else:
                self.close()
            event.accept()
            return
        # S5：--wid 嵌入后键盘焦点在 Qt 播放窗口（mpv 子窗口收不到键盘——
        # mpv 官方承认的结构性缺陷）→ Qt 侧捕获快捷键 → 转发 mpv 命令。
        # 走既有全链（overlay 按钮 click / 滑块 setValue / controller 方法），
        # 不复制业务；全部 try/except 兜底——键盘异常不崩播放。
        try:
            if key == Qt.Key_Space:
                self.overlay.btn_toggle.click()          # 播放/暂停
            elif key == Qt.Key_Left:
                self._movie.seek(-5)                     # 快退 5s
            elif key == Qt.Key_Right:
                self._movie.seek(5)                      # 快进 5s
            elif key == Qt.Key_Up:
                self._step_volume(5)                     # 音量 +5
            elif key == Qt.Key_Down:
                self._step_volume(-5)                    # 音量 -5
            elif key == Qt.Key_F:
                self.request_fullscreen() if not self._fullscreen \
                    else self.showNormal()               # 全屏切换
            elif key == Qt.Key_M:
                self.overlay.btn_mute.click()            # 静音切换
            else:
                super().keyPressEvent(event)
                return
            event.accept()
            return
        except Exception:
            pass
        super().keyPressEvent(event)

    def _step_volume(self, delta):
        """音量 ±5（读 overlay 滑块现值，clamp 0-100；setValue 走 _on_volume 全链）。"""
        try:
            v = max(0, min(100, self.overlay.slider_vol.value() + delta))
            self.overlay.slider_vol.setValue(v)
        except Exception:
            pass

    def showFullScreen(self):
        self._fullscreen = True
        super().showFullScreen()
        self._position_overlay()
        self._position_sidebar()

    def showNormal(self):
        self._fullscreen = False
        super().showNormal()
        self._position_overlay()
        self._position_sidebar()

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
        if self.sidebar.isVisible():
            self._position_sidebar()
        super().moveEvent(event)

    def resizeEvent(self, event):
        self._position_overlay()
        if self.sidebar.isVisible():
            self._position_sidebar()
        super().resizeEvent(event)
