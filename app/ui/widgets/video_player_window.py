"""电影内嵌播放窗口（S0 骨架 + S1 最小内嵌）。

架构：Qt 顶层播放窗口 + VideoContainer（WA_NativeWindow，mpv --wid 渲染目标）
+ 独立 ControlOverlay 顶层窗口（Qt::Tool | StaysOnTop，天然浮在 mpv 之上——
z-order 原型实证：SetWindowPos 调兄弟 z-order 无效，独立顶层窗口是可靠解）。

职责边界（硬约束）：播放窗口只做"呈现 + 发控制指令"，业务全在 MovieController
（唯一控制器——搜索/互斥/连播/字幕/状态写入）。本文件零业务复制。

S1 范围：容器 + 占位 overlay（⏯ 暂停/继续 + 关闭）+ 标题栏进度轮询 +
关窗=停播（hide 保留 winId，复用）+ Esc（全屏退全屏 / 非全屏关窗）。
进度条/倍速/侧栏等 S2+ 补。
"""
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


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
    """独立顶层控制层（Qt::Tool | Frameless | StaysOnTop）。
    S1 只放占位条 + ⏯ 暂停/继续 + ✕ 关闭；S2 填进度条/倍速/音量等。"""

    def __init__(self, window):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._window = window
        self.setAttribute(Qt.WA_NativeWindow)
        self.setStyleSheet("background:rgba(28,28,28,210); border:1px solid #555;")
        self.resize(420, 52)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        self.lbl = QLabel("内嵌播放")
        self.lbl.setStyleSheet("color:#eee; font-size:12px;")
        self.btn_toggle = QPushButton("⏯")
        self.btn_toggle.setFixedWidth(44)
        self.btn_close = QPushButton("✕ 关闭")
        self.btn_close.setFixedWidth(72)
        lay.addWidget(self.lbl, 1)
        lay.addWidget(self.btn_toggle)
        lay.addWidget(self.btn_close)

        self.btn_toggle.clicked.connect(window.toggle_pause)
        self.btn_close.clicked.connect(window.request_close)


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

        # 独立顶层 overlay（S1 占位条：⏯/关闭）
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

    def request_close(self):
        self.close()

    # ---- 控制（只转发，不复制业务） ----

    def toggle_pause(self):
        try:
            if self._movie.is_playing():
                self._movie.toggle_pause()
        except Exception:
            pass

    # ---- 轮询（进度标题 + 连播检测；异常静默降级） ----

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
                self.overlay.lbl.setText(f"{name}  {_fmt(pos)}/{_fmt(dur)}")
            else:
                self.setWindowTitle(f"{name}  {_fmt(pos)}/0:00 - {self._title_base}")
                self.overlay.lbl.setText(f"{name}  {_fmt(pos)}/0:00")
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

    # ---- overlay 定位（跟随窗口 move/resize/全屏） ----

    def _position_overlay(self):
        geo = self.geometry()
        x = geo.x() + (geo.width() - self.overlay.width()) // 2
        y = geo.y() + geo.height() - self.overlay.height() - 30
        self.overlay.move(x, y)
        self.overlay.raise_()

    def moveEvent(self, event):
        self._position_overlay()
        super().moveEvent(event)

    def resizeEvent(self, event):
        self._position_overlay()
        super().resizeEvent(event)
