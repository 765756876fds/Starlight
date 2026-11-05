"""电影内嵌播放窗口（S0-S8）。

架构：Qt 顶层播放窗口 + VideoContainer（WA_NativeWindow，mpv --wid 渲染目标）
+ ControlOverlay 独立顶层窗口（Qt::Tool | Frameless，owner = 播放窗口——S7
去 StaysOnTop：owned window 天然浮在 mpv 之上、播放窗口被盖时跟随一起被盖，
z-order 原型实证：SetWindowPos 调兄弟 z-order 无效，独立顶层窗口是可靠解）。
EpisodeSidebar（S4，同 owned 模式）右缘选集。

职责边界（硬约束）：播放窗口只做"呈现 + 发控制指令"，业务全在 MovieController
（唯一控制器——搜索/互斥/连播/字幕/状态写入/崩溃恢复）。本文件零业务复制。

控制条（S2 起，S6/S7 对标 B站）：两行布局——进度条上行（拖动预览/松手 seek）、
按钮下行（播放/下一集/时间/音量/倍速/字幕/全屏，Feather/Lucide 线性图标）；
鼠标浮现/静止 3s 隐藏（S3）；宽度跟随窗口（resizeEvent 重定位 + 重设宽）。
键盘焦点在 Qt 侧捕获（S5：容器 StrongFocus、控件操作后焦点归还容器；
NOACTIVATE 方案 S2 实测桌面不显示，已整体弃用）。
mpv 崩溃恢复（S8）：窗口 _tick 轮询检测。
"""
from PySide6.QtCore import QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QLinearGradient, QPainter

import threading
from ...core.notifications import notify
from ..theme.tokens import Tokens
import os
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSlider,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.video_icons import icon as _icon

from .clickable_slider import ClickableSlider

# 倍速档位（对齐 B站/爱奇艺标准 9 档；与 movie_panel 同源值）
_SPEED_STEPS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]

# overlay 固定高度（S7 两行布局：进度条行 + 按钮行 ≈ 72；禁依赖未 show 时
# height()=480 撑成灰块）
_OVERLAY_H = 72

# S3：鼠标静止 3s 隐藏控制条（B站式观影沉浸；hover 控制条不隐藏）
_OVERLAY_HIDE_DELAY_MS = 3000

# S4：选集侧栏——右缘触发宽度 / 侧栏宽 / 静止 3s 隐藏
_SIDEBAR_EDGE_PX = 60
_SIDEBAR_W = 150
_SIDEBAR_HIDE_DELAY_MS = 3000


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
        self.setProperty("themeIndependent", True)  # The actual movie canvas stays black.
        self._win_id = None
        self._window = parent              # 播放窗口（转发鼠标活动）
        self.setMouseTracking(True)        # 无按键移动也收 mouseMoveEvent
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        # 使用 Qt/Windows 当前双击间隔，避免固定 220ms 过早执行单击，
        # 导致一次双击被拆成“暂停后继续”。
        self._click_timer.setInterval(QApplication.styleHints().mouseDoubleClickInterval())
        self._click_timer.timeout.connect(self._on_single_click)
        # S5：点击画面 → 焦点到容器（--wid 嵌入后键盘焦点在 Qt 侧；容器聚焦
        # 后按键沿父链冒泡到播放窗口 keyPressEvent —— 键盘控制可用）
        self.setFocusPolicy(Qt.StrongFocus)

    def mousePressEvent(self, event):
        # S5：点画面收焦点（键盘事件到 Qt，不发 mpv——mpv 收不到 --wid 子窗口键盘）
        self.setFocus()
        if event.button() == Qt.LeftButton:
            self._click_timer.start()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._click_timer.stop()
        super().mouseDoubleClickEvent(event)

    def _on_single_click(self):
        if self._window is not None:
            self._window.toggle_pause()

    def native_id(self):
        """容器 HWND（uint32——win32 句柄 32 位，负值 mpv 拒收）。
        WA_NativeWindow 强制 native，首次调用即创建句柄并缓存。"""
        if self._win_id is None:
            self._win_id = int(self.winId()) & 0xFFFFFFFF
        return self._win_id

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
    """独立顶层控制层（Qt::Tool | Frameless，owner = 播放窗口）——B站式底部控制条。
    只发控制指令给 movie_controller（mpv IPC），零业务复制。
    S6 布局（对齐 B站）：播放/暂停(SVG) · 下一集(SVG) · 进度条(拖动预览/松手 seek)
    · 时间 · 音量滑块+静音(SVG 三态) · 倍速(弹菜单 9 档当前档高亮) · 字幕(SVG，
    弹 Assrt 对话框) · 全屏(SVG，进入/退出切换)。停止按钮已取消（用户：停止=关窗）。
    S7：去永久置顶（owner 跟随播放窗口——被盖一起盖）；渐变透明背景；两行布局
    （进度条独立一行在上、按钮行在下，对齐 B站）。"""

    def __init__(self, window):
        # S7：去 WindowStaysOnTopHint（= WS_EX_TOPMOST 全局永久置顶——别的软件盖住
        # 视频窗口时 overlay 仍浮在最上，用户实机反馈）。改 Qt parent = 播放窗口 →
        # Windows owned window 关系（owner = 播放窗口）：浮在 owner 及其子窗口
        # （容器/mpv）之上，但播放窗口被其他应用盖住时 overlay 跟随一起被盖。
        super().__init__(window, Qt.Tool | Qt.FramelessWindowHint)
        self._window = window
        self._movie = window._movie
        self.setAttribute(Qt.WA_NativeWindow)
        # S2 修复：撤 WA_ShowWithoutActivating（Qt 在 Windows 用 SW_SHOWNA 显示且永不激活，
        # 与 NOACTIVATE 组合导致工具窗口在桌面不显示——回到 S1 已验证显示路径）。
        # 焦点管理（NOACTIVATE）整体推到 S5；本批接受"点 overlay 会抢 mpv 焦点"。
        self.setFixedHeight(_OVERLAY_H)
        # S7：透明背景（B站式：底部深、往上渐变透明，无边框——去"黑框"）。
        # 注：QSS background 在 WA_TranslucentBackground 顶层窗口上不渲染（实锤
        # 对照 alpha=0）——渐变在 paintEvent 手绘（透明窗口标准做法）；QSS 顶层
        # 显式 background:transparent 防实心填充残留。
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(
            "background:transparent;"
            f" color:{Tokens.TEXT_PRIMARY}; font-size:12px;"
            f" QPushButton{{background:transparent;border:none;color:{Tokens.TEXT_PRIMARY};"
            " border-radius:4px;}"
            f" QPushButton:hover{{background:{Tokens.BG_HOVER};color:{Tokens.TEXT_PRIMARY};}}"
            f" QSlider::groove:horizontal{{height:3px;background:{Tokens.BORDER_LIGHT};}}"
            f" QSlider::sub-page:horizontal{{background:{Tokens.ACCENT};}}"
            " QSlider::handle:horizontal{width:10px;margin:-4px 0;"
            f" background:{Tokens.ACCENT_HOVER};border-radius:5px;}}")
        self._speed_idx = 2          # 1.0x 默认档
        self._paused = False
        self._muted = False
        self._dragging = False
        self._switching = False  # 切集/加载守卫：置位期间不渲染旧集进度（_tick 拦截）
        self._vol_state = "high"     # S6：音量图标三态（high/low/muted）
        self._play_state = "play"    # S6：播放/暂停图标态
        # S3 鼠标浮现/隐藏：overlay 是独立顶层窗口——鼠标在 overlay 上时播放窗口
        # 收不到 mouseMoveEvent，overlay 自行跟踪并通知窗口（防操作中消失）。
        self.setMouseTracking(True)

        # S7 两行布局（对齐 B站）：行1 = 进度条独立一行（横跨整行）；行2 = 按钮行。
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(4)

        # ---- 行1：进度条（独立一行，横跨整行，对齐 B站） ----
        self.row_progress = QWidget(self)
        row1 = QHBoxLayout(self.row_progress)
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(0)

        # 进度条（点击/拖动预览，松手才 seek；ClickableSlider 支持点击精确跳转）
        self.slider_pos = ClickableSlider(Qt.Horizontal)
        self.slider_pos.setRange(0, 1000)
        self.slider_pos.sliderPressed.connect(self._on_slider_pressed)
        self.slider_pos.sliderReleased.connect(self._on_slider_released)
        self.slider_pos.valueChanged.connect(self._on_slider_value)
        row1.addWidget(self.slider_pos, 1)
        lay.addWidget(self.row_progress)

        # ---- 行2：按钮行（对齐 B站：播放 · 下一集 · 时间 · 音量 · 倍速 · 字幕 · 全屏） ----
        self.row_buttons = QWidget(self)
        row2 = QHBoxLayout(self.row_buttons)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)

        # 播放/暂停（S6 SVG 图标切换）
        self.btn_toggle = QPushButton()
        self.btn_toggle.setFixedSize(34, 34)
        self.btn_toggle.setToolTip("暂停/继续")
        self.btn_toggle.setIcon(_icon("play"))
        self.btn_toggle.clicked.connect(self._on_toggle)
        row2.addWidget(self.btn_toggle)

        # 下一集（S6 新增；数据在窗口 _current_item，复用 play 链）
        self.btn_next = QPushButton()
        self.btn_next.setFixedSize(34, 34)
        self.btn_next.setToolTip("下一集")
        self.btn_next.setIcon(_icon("skip_forward"))
        self.btn_next.clicked.connect(window.play_next)
        row2.addWidget(self.btn_next)

        # 时间
        self.lbl_time = QLabel("0:00 / 0:00")
        row2.addWidget(self.lbl_time)

        # 弹性空隙：时间与右侧按钮组分隔（对齐 B站）
        row2.addStretch()

        # 音量滑块 + 静音（S6 SVG 三态：高/低/静音）
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(70)
        self.slider_vol.setFixedWidth(80)
        self.slider_vol.setToolTip("音量")
        self.slider_vol.valueChanged.connect(self._on_volume)
        row2.addWidget(self.slider_vol)

        self.btn_mute = QPushButton()
        self.btn_mute.setFixedSize(30, 30)
        self.btn_mute.setToolTip("静音")
        self.btn_mute.setIcon(_icon("volume_high"))
        self.btn_mute.clicked.connect(self._on_mute)
        row2.addWidget(self.btn_mute)

        # 倍速（弹菜单 9 档；B站同款文字按钮）
        self.btn_speed = QPushButton("倍速 1x")
        self.btn_speed.setToolTip("倍速（9 档）")
        self.btn_speed.clicked.connect(self._on_speed)
        row2.addWidget(self.btn_speed)

        # 字幕（S6 新增：弹 Assrt 字幕对话框——复用面板 _SubtitleDialog）
        self.btn_subtitle = QPushButton()
        self.btn_subtitle.setFixedSize(30, 30)
        self.btn_subtitle.setToolTip("字幕")
        self.btn_subtitle.setIcon(_icon("captions"))
        self.btn_subtitle.clicked.connect(window.request_subtitle)
        row2.addWidget(self.btn_subtitle)

        # 全屏（S6 SVG：进入/退出切换）
        self.btn_fullscreen = QPushButton()
        self.btn_fullscreen.setFixedSize(30, 30)
        self.btn_fullscreen.setToolTip("全屏")
        self.btn_fullscreen.setIcon(_icon("maximize"))
        self.btn_fullscreen.clicked.connect(window.request_fullscreen)
        row2.addWidget(self.btn_fullscreen)

        lay.addWidget(self.row_buttons)

        # S5：任何控件操作后焦点归还容器——防"点了按钮后键盘失效"
        # （--wid 嵌入键盘在 Qt 侧；overlay 独立顶层点击不天然给容器焦点，
        # 不用 NOACTIVATE——S2 教训：NOACTIVATE 组合桌面不显示）
        self._restore_focus = self._make_restore_focus(window)
        for w in (self.btn_toggle, self.btn_next, self.btn_speed,
                  self.btn_mute, self.btn_subtitle, self.btn_fullscreen):
            w.clicked.connect(self._restore_focus)
        self.slider_pos.sliderReleased.connect(self._restore_focus)
        self.slider_vol.sliderReleased.connect(self._restore_focus)

    # ---- S7：B站式渐变透明背景（QSS 在透明顶层窗口不渲染，手绘渐变） ----

    def paintEvent(self, event):
        """背景：底部深 rgba(24,24,24,235) → 顶部完全透明（视频透出，去黑框）。
        WA_TranslucentBackground 下 QSS background 不渲染（实锤对照 alpha=0），
        必须 paintEvent 手绘；children（按钮/滑块）绘制在本层之上。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, self.height(), 0, 0)
        background = QColor(Tokens.OVERLAY_BACKGROUND)
        for position, alpha in ((0.0, 245), (0.5, 215), (1.0, 0)):
            background.setAlpha(alpha)
            grad.setColorAt(position, background)
        painter.fillRect(self.rect(), grad)
        painter.end()

    # ---- S6：状态图标切换（线性 SVG；按钮文本为空，icon 承载状态） ----

    def set_play_icon(self, paused: bool):
        """播放/暂停图标切换（键盘/按钮共用入口）。"""
        self._play_state = "pause" if paused else "play"
        self.btn_toggle.setIcon(_icon(self._play_state))

    def set_volume_icon(self):
        """音量三态图标：静音 → volume_muted；否则按滑块值 high/low。"""
        if self._muted:
            self._vol_state = "muted"
        else:
            self._vol_state = "low" if self.slider_vol.value() <= 20 else "high"
        self.btn_mute.setIcon(_icon({
            "muted": "volume_muted", "low": "volume_low",
            "high": "volume_high"}[self._vol_state]))

    def set_fullscreen_icon(self, fullscreen: bool):
        """全屏进入/退出图标切换。"""
        self.btn_fullscreen.setIcon(_icon("minimize" if fullscreen else "maximize"))

    def set_next_enabled(self, enabled: bool):
        """最后一集 → 下一集禁用（无操作）。"""
        self.btn_next.setEnabled(enabled)
        self.btn_next.setToolTip("下一集" if enabled else "已是最后一集")

    @staticmethod
    def _make_restore_focus(window):
        """返回"焦点还给容器"的调用（防跨类私有依赖；失败静默）。"""
        def _restore():
            try:
                window.container.setFocus()
            except Exception:
                pass
        return _restore

    # ---- S3：overlay 鼠标跟踪（独立顶层窗口，鼠标在其上时播放窗口收不到事件） ----

    def enterEvent(self, event):
        # 鼠标进入控制条 → 通知窗口"hover 中"（停止隐藏计时，防操作中消失）
        try:
            self._window.notify_overlay_hover(True)
        except Exception:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event):
        try:
            self._window.notify_overlay_hover(False)
        except Exception:
            pass
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        # overlay 上活动 = 仍在操作 → 通知窗口浮现 + 重置隐藏计时
        try:
            self._window.notify_mouse_activity()
        except Exception:
            pass
        super().mouseMoveEvent(event)

    # ---- 控制转发（只发指令，不复制业务） ----

    def _on_toggle(self):
        self._paused = not self._paused
        self.set_play_icon(self._paused)      # S6：SVG 图标切换（按钮文本为空）
        try:
            self._movie.toggle_pause()
        except Exception:
            pass

    def _on_slider_pressed(self):
        self._dragging = True

    def _on_slider_released(self):
        self._dragging = False
        # seek 整体丢后台线程：get_duration + seek 两次 mpv IPC 绝不阻塞主线程
        # （点击跳转同样经 sliderReleased 信号到达，复用同一后台通道）
        threading.Thread(target=self._do_seek, daemon=True, name="overlay-seek").start()

    def _do_seek(self):
        """后台线程执行 seek（IPC 出主线程；失败静默，不炸线程）。"""
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
        self.set_volume_icon()      # S6：音量三态图标（高/低/静音）

    def _on_mute(self):
        self._muted = not self._muted
        self.set_volume_icon()      # S6：静音图标切换（按钮文本为空）
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
    Qt::Tool | Frameless，owner = 播放窗口；S7 去永久置顶——被盖跟随播放窗口，
    但仍浮在 mpv 画面之上，原型验证 z-order 可行）。

    贴播放窗口右缘纵向列出分集（第1集/第2集…，QListWidget 自带滚动——
    剧集几十集可滚）；当前集高亮；点击切集只发指令（复用窗口 play 链）。
    单集（电影）不显示。浮现/隐藏由窗口侧控制（右缘鼠标触发 + 3s 静止隐藏 +
    hover 不隐藏）——本类只渲染/交互/通知 hover 状态。
    """

    def __init__(self, window):
        # S7：去 WindowStaysOnTopHint（同 overlay——全局永久置顶的根因），
        # Qt parent = 播放窗口 → owned window：浮在 mpv 之上、被盖跟随播放窗口。
        super().__init__(window, Qt.Tool | Qt.FramelessWindowHint)
        self._window = window
        self.setAttribute(Qt.WA_NativeWindow)
        self.setFixedWidth(_SIDEBAR_W)
        self.setStyleSheet(
            f"background:{Tokens.CARD}; border:1px solid {Tokens.BORDER};"
            f" color:{Tokens.TEXT_PRIMARY}; font-size:12px;")
        self._episodes = []          # [(name, url), ...]
        self._current = 0

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget{background:transparent;border:none;}"
            "QListWidget::item{padding:6px 10px;}"
            f"QListWidget::item:selected{{background:{Tokens.BG_ACTIVE};color:{Tokens.TEXT_PRIMARY};}}")
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
            self._window.notify_sidebar_hover(True)
        except Exception:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event):
        try:
            self._window.notify_sidebar_hover(False)
        except Exception:
            pass
        super().leaveEvent(event)


class VideoPlayerWindow(QWidget):
    """内嵌播放窗口：容器嵌 mpv 画面 + overlay 控制层（独立顶层，不被盖）。
    关窗 = 停播 + 隐藏（不销毁，winId 保留——下次播放直接复用窗口/内嵌目标）。
    Esc：全屏时退全屏；非全屏时关窗（停播）。"""

    # 停播信号（close / 播完 / mpv 异常自愈失败时发，面板清 embed 状态）
    stopped = Signal()
    _subtitle_finished = Signal(int, object)

    def __init__(self, controller, title="电影播放"):
        super().__init__(None)
        self._movie = controller
        self.setWindowTitle(title)
        self._qs = QSettings("LoLMusicAgent", "VideoPlayer")
        saved = self._qs.value("size")
        if saved is not None:
            try:
                self.resize(int(saved.width()), int(saved.height()))
            except Exception:
                self.resize(960, 540)
        else:
            self.resize(960, 540)
        self._fullscreen = False
        self._playing = False
        self._title_base = title
        self._current_item = None        # S4：当前播放 item（切集复用）
        self._current_index = 0          # S6：当前集索引（下一集定位）
        # S8：mpv 崩溃自愈状态（恢复中 tick 只轮询恢复结果，不走正常进度刷新）
        self._recovering = False
        self._subtitle_busy = False
        self._subtitle_request = 0
        self._subtitle_menu = None
        self._subtitle_finished.connect(self._on_subtitle_finished, Qt.QueuedConnection)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.container = VideoContainer(self)
        lay.addWidget(self.container)

        # S8：崩溃恢复提示（mpv 崩后容器上无子窗口，label 可见；恢复成功/收尾隐藏）
        self._recover_label = QLabel(self)
        self._recover_label.setAlignment(Qt.AlignCenter)
        self._recover_label.setStyleSheet(
            f"color:{Tokens.TEXT_PRIMARY};font-size:16px;background:{Tokens.CARD};"
            "border-radius:8px;padding:10px 18px;")
        self._recover_label.hide()

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
        self._invalidate_subtitle_menu()
        self._movie.set_embed_wid(self.container.native_id())
        ok = self._movie.play(item, index=index)
        if ok:
            name = (item.get("vod_name") or "")[:20]
            self.setWindowTitle(f"{name} - {self._title_base}")
            self._playing = True
            # 切集/加载守卫：新集起播前（pos 回到 0）不渲染旧集进度，防倒退
            self._switching = True
            self._current_item = item           # S4：切集复用
            # S4：剧集列表（split_play_url 拆解；单集 → 侧栏隐藏）
            try:
                from app.movie.movie_controller import split_play_url
                eps = split_play_url(item.get("play_url") or "")
            except Exception:
                eps = []
            self.sidebar.set_episodes(eps, index or 0)
            self._current_index = index or 0    # S6：下一集定位
            # S6：下一集按钮状态（最后一集禁用）
            self.overlay.set_next_enabled(self._current_index + 1 < self.sidebar.episode_count())
            self.overlay.set_play_icon(False)   # S6：新片播放态（图标同步）
            self._apply_mpv_mouse_passthrough()   # S3：mpv 子窗口鼠标穿透（幂等）
            self._show_overlay()          # S3：浮现 + 启动 3s 隐藏计时
            self._timer.start()
            self._position_overlay()
        return ok

    def show_for_playback(self):
        """显示播放窗口；若被最小化，先恢复为正常窗口。"""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()

    def attach_current(self):
        """外部已开始播放（语音路径：controller.play 已用注入的 winId 起 mpv），
        窗口负责接管轮询/overlay 呈现。"""
        if not self._movie.is_playing():
            return
        self._invalidate_subtitle_menu()
        self._playing = True
        self._apply_mpv_mouse_passthrough()   # S3：mpv 子窗口鼠标穿透（幂等）
        self._show_overlay()              # S3：浮现 + 启动 3s 隐藏计时
        self._timer.start()
        self._position_overlay()

    # ---- 窗口动作（overlay 按钮入口；只呈现，不复制业务） ----

    def request_close(self):
        self.close()

    def play_next(self):
        """下一集：复用 play 链切集（index+1）；最后一集无操作。"""
        if self._current_item is None:
            return
        if self._current_index + 1 >= self.sidebar.episode_count():
            return
        self.play(self._current_item, index=self._current_index + 1)

    def request_subtitle(self):
        """IPC 查询放在后台；菜单和反馈只在 GUI 线程创建/更新。"""
        self._start_subtitle_task("menu", self._movie.subtitle_state)

    def _start_subtitle_task(self, kind, operation):
        if self._subtitle_busy:
            return
        self._subtitle_busy = True
        self.overlay.btn_subtitle.setEnabled(False)
        self._subtitle_request += 1
        request = self._subtitle_request
        finished = self._subtitle_finished

        def run():
            try:
                result = operation()
            except Exception as e:
                print(f"[播放窗口] 字幕操作异常: {type(e).__name__}")
                result = None if kind == "menu" else (False, "字幕操作失败，请稍后重试")
            try:
                finished.emit(request, (kind, result))
            except RuntimeError:
                pass  # Qt owner 已销毁：不再触碰窗口。

        threading.Thread(target=run, name="MovieSubtitle", daemon=True).start()

    @Slot(int, object)
    def _on_subtitle_finished(self, request, payload):
        self._subtitle_busy = False
        self.overlay.btn_subtitle.setEnabled(True)
        if request != self._subtitle_request:
            return
        kind, result = payload
        if kind == "menu":
            menu = self._build_subtitle_menu(result)
            self._hide_timer.stop()
            menu.popup(self.overlay.btn_subtitle.mapToGlobal(self.overlay.btn_subtitle.rect().topLeft()))
        else:
            ok, message = result
            if ok and kind == "load":
                message = f"字幕已加载：{os.path.basename(message)}"
            self._subtitle_feedback(message)
            notify("字幕已加载" if ok and kind == "load" else "字幕设置已更新" if ok else "字幕操作没有完成",
                   message, status="success" if ok is True else "failed", source="movie",
                   advice="请检查字幕文件是否有效，视频是否仍在播放" if not ok else "")

    def _build_subtitle_menu(self, state):
        if self._subtitle_menu is not None:
            self._subtitle_menu.close()
            self._subtitle_menu.deleteLater()
        menu = QMenu(self.overlay)
        self._subtitle_menu = menu
        menu.setStyleSheet(f"QMenu{{background:{Tokens.BG_ELEVATED};color:{Tokens.TEXT_PRIMARY};border:1px solid {Tokens.BORDER};}}"
                          f"QMenu::item{{padding:7px 18px;}}QMenu::item:selected{{background:{Tokens.BG_ACTIVE};}}")
        media = state["media"] if state else None
        local = menu.addAction("加载本地字幕…")
        local.setEnabled(state is not None)
        local.triggered.connect(lambda: self._choose_local_subtitle(media))
        online = menu.addAction("搜索在线字幕（Assrt）…")
        online.triggered.connect(self._request_assrt_subtitle)
        menu.addSeparator()
        visible = menu.addAction("显示字幕")
        visible.setCheckable(True)
        visible.setChecked(bool(state and state["visible"]))
        visible.setEnabled(state is not None)
        visible.triggered.connect(lambda checked: self._start_subtitle_task(
            "control", lambda: self._movie.set_subtitle_visible(checked, expected_media=media)))
        tracks = state["tracks"] if state else []
        if not tracks:
            empty = menu.addAction("当前没有字幕" if state else "视频尚未就绪或字幕状态不可用")
            empty.setEnabled(False)
        for track in tracks:
            sid = track["id"]
            name = track.get("title") or os.path.basename(track.get("external-filename") or "") or f"字幕 {sid}"
            if track.get("lang"):
                name += f" [{track['lang']}]"
            action = menu.addAction(name.replace("&", "&&"))
            action.setCheckable(True)
            action.setChecked(state["sid"] == sid)
            action.triggered.connect(lambda checked=False, sid=sid: self._start_subtitle_task(
                "control", lambda: self._movie.select_subtitle(sid, expected_media=media)))
        menu.aboutToHide.connect(lambda: self._hide_timer.start(_OVERLAY_HIDE_DELAY_MS)
                                 if self._playing else None)
        return menu

    def _choose_local_subtitle(self, media):
        filename, _ = QFileDialog.getOpenFileName(
            self, "加载本地字幕", "", "字幕文件 (*.srt *.ass *.ssa *.vtt);;所有文件 (*)")
        if filename:
            self._start_subtitle_task("load", lambda: self._movie.load_local_subtitle(
                filename, expected_media=media))

    def _subtitle_feedback(self, message):
        QToolTip.showText(self.overlay.btn_subtitle.mapToGlobal(
            self.overlay.btn_subtitle.rect().topLeft()), message, self, self.rect(), 5000)

    def _invalidate_subtitle_menu(self):
        self._subtitle_request += 1
        if self._subtitle_menu is not None:
            self._subtitle_menu.close()

    def _request_assrt_subtitle(self):
        """保留原有 Assrt 对话框及其下载链路。"""
        try:
            from app.ui.panels.movie_panel import _SubtitleDialog
            kw = (self._current_item or {}).get("vod_name") or ""
            dlg = _SubtitleDialog(self._movie, kw, self)
            dlg.exec()
        except Exception as e:
            print(f"[播放窗口] 字幕对话框异常: {e}")

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
                paused = self._movie.toggle_pause()
                if paused is not None:
                    self.overlay.set_play_icon(paused)
        except Exception:
            pass

    # ---- 轮询（连播检测 + 进度/overlay 刷新；异常静默降级） ----

    def _tick(self):
        try:
            # V2-4.6.8：同步语音/外部修改的电影音量到播放窗口 overlay 滑块
            try:
                v = self._movie.get_volume()
                if v is not None and self.overlay.slider_vol.value() != v:
                    self.overlay.slider_vol.setValue(v)
            except Exception:
                pass
            # 连播检测先行：播完一集 idle 上升沿 → 下一集（is_playing 恢复 True）。
            # 若先查 is_playing，播完瞬间会误判"停播"直接收尾，连播永远起不来。
            self._movie.tick()
            # S8 崩溃自愈状态机：
            if self._recovering:
                self._recover_poll()
                return
            if not self._movie.is_playing():
                # S8：mpv 崩溃（进程死 + 仍有播放上下文）→ 触发自愈；
                # 否则为正常停播（播完/用户停止）→ 原收尾路径。
                if self._playing and self._movie.is_crashed():
                    self._start_recover()
                    return
                self._on_stopped()
                return
            pos = self._movie.get_position()
            dur = self._movie.get_duration()
            it = self._movie.current_item() or {}
            name = (it.get("vod_name") or "")[:20]
            # 切集/加载守卫：切集后新集 loadfile 起播前（pos 回到 0 之前），
            # mpv 可能仍在报旧集 position——禁止写入进度条，防倒退。
            # 起播确认（is_playing 且 pos>0）后放开守卫。
            if self._switching:
                if not (self._movie.is_playing() and pos > 0):
                    return
                self._switching = False
            if dur > 0:
                self.setWindowTitle(f"{name}  {_fmt(pos)}/{_fmt(dur)} - {self._title_base}")
            else:
                self.setWindowTitle(f"{name}  {_fmt(pos)}/0:00 - {self._title_base}")
            self.overlay.refresh(pos, dur)
        except Exception:
            pass

    # ---- S8：mpv 崩溃自愈（检测 → 提示 → controller 重启 → 轮询恢复结果） ----

    def _start_recover(self):
        """检测到崩溃：容器提示 + 调 controller 自愈（防重入）。
        超限放弃/无可恢复上下文（restart_current 返回 False）→ 提示失败 + 收尾。"""
        if self._recovering:
            return
        self._recovering = True
        self._show_recover_label("播放器异常，正在恢复…")
        ok = self._movie.restart_current(wid=self.container.native_id())
        if not ok:
            self._recovering = False
            self._show_recover_label("播放器异常，自动恢复失败")
            self._flash_recover_failed()
            self._on_stopped()

    def _recover_poll(self):
        """恢复中轮询（tick 每 500ms）：
        成功（新 mpv 起来 + loadfile → is_playing True）→ 重设穿透回正常态；
        超限放弃 → 提示失败 + 收尾。"""
        try:
            if self._movie.is_playing():
                self._recovering = False
                self._hide_recover_label()
                self._apply_mpv_mouse_passthrough()   # 新 mpv 进程 → 重设穿透（幂等）
                self._show_overlay()
            elif self._movie.is_gave_up():
                self._recovering = False
                self._show_recover_label("播放器异常，自动恢复失败")
                self._flash_recover_failed()
                self._on_stopped()
        except Exception:
            pass

    def _flash_recover_failed(self):
        """失败提示短暂停留（3s）后消失；窗口若还在播则定时器自清。"""
        QTimer.singleShot(3000, self._hide_recover_label)

    def _show_recover_label(self, text):
        self._recover_label.setText(text)
        self._recover_label.adjustSize()
        self._recover_label.move(
            max(8, (self.width() - self._recover_label.width()) // 2),
            max(8, (self.height() - self._recover_label.height()) // 2))
        self._recover_label.show()
        self._recover_label.raise_()

    def _hide_recover_label(self):
        self._recover_label.hide()

    def _on_stopped(self):
        """停播（播完/停止/异常）：停轮询 + 隐藏 overlay/侧栏 + 通知面板。"""
        self._invalidate_subtitle_menu()
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

    def notify_overlay_hover(self, hovering: bool):
        """控制条 hover 状态（公开接口——overlay 跨类不摸私有方法）。"""
        self._on_overlay_hover(hovering)

    def notify_sidebar_hover(self, hovering: bool):
        """侧栏 hover 状态（公开接口——sidebar 跨类不摸私有方法）。"""
        self._on_sidebar_hover(hovering)

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
        if self._overlay_hover or (self._subtitle_menu is not None and self._subtitle_menu.isVisible()):
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
        # 尺寸记忆：关窗（停播隐藏）时保存当前宽高，下次打开恢复
        try:
            self._qs.setValue("size", self.size())
        except Exception:
            pass
        try:
            if self._playing or self._movie.is_playing():
                self._movie.stop()
        except Exception:
            pass
        # S8：恢复中关窗 → 停止恢复（controller.stop 清上下文，worker 守卫放弃恢复）
        self._recovering = False
        self._hide_recover_label()
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
        self.overlay.set_fullscreen_icon(True)   # S6：进入全屏图标
        self._position_overlay()
        self._position_sidebar()

    def showNormal(self):
        self._fullscreen = False
        super().showNormal()
        self.overlay.set_fullscreen_icon(False)  # S6：退出全屏图标
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
