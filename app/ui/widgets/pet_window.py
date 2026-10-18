"""桌面宠物（GIF 版）：QWidget + QMovie 播放待机 GIF。已彻底废弃 Live2D/OpenGL。

- 无边框置顶透明窗口，内部 QLabel 铺满窗口播放 QMovie（idle.gif）
- 鼠标按住拖拽移动窗口
- 贴边隐藏：松手时若窗口贴近屏幕边缘 15px 内 → 完全隐藏（hide()）
- 全屏检测：每 2 秒检测前台窗口是否全屏，全屏则隐藏桌宠，退出全屏则恢复显示
- 动作切换：play_motion("idle"/"listen"/"ok")，素材缺失时回退 idle

不依赖任何业务数据/线程；素材缺失时窗口仍可显示（QLabel 空）。
"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QMovie
from PySide6.QtWidgets import QLabel, QWidget

_ASSETS_DIR = Path(__file__).resolve().parents[3] / "assets" / "pet"
_DEFAULT_MOTIONS = ("idle", "listen", "ok")
_EDGE_SNAP = 15          # 松手时距屏幕边缘多少 px 内视为"贴边"
_FULLSCREEN_CHECK_MS = 2000  # 全屏检测频率

_MOTION_FILE = {m: _ASSETS_DIR / f"{m}.gif" for m in _DEFAULT_MOTIONS}


def _is_fullscreen_window(hwnd) -> bool:
    """Windows：判断前台窗口是否铺满屏幕（粗略全屏检测）。"""
    try:
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return False
        scr = QGuiApplication.primaryScreen()
        if scr is None:
            return False
        geo = scr.geometry()
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        return w >= geo.width() - 4 and h >= geo.height() - 4
    except Exception:
        return False


class PetWindow(QWidget):
    """桌宠外壳：无边框置顶透明 + QMovie 待机动画 + 拖拽 + 贴边隐藏 + 全屏检测。"""

    def __init__(self, motions: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(280, 360)

        self._motions = dict(_MOTION_FILE)
        if motions:
            self._motions.update(motions)
        self._current = "idle"
        self._movie: QMovie | None = None

        # 铺满窗口的 QLabel 播放待机 GIF
        self._label = QLabel(self)
        self._label.setGeometry(0, 0, self.width(), self.height())
        self._label.setScaledContents(True)
        self._label.setAttribute(Qt.WA_TranslucentBackground)

        # 拖拽与贴边状态
        self._drag_offset: QPoint | None = None
        self._hidden_by_edge = False   # 松手贴边已 hide（靠开关重新显示）
        self._hidden_by_fullscreen = False  # 全屏期间已隐藏（退出全屏自动恢复）

        # 全屏检测
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(_FULLSCREEN_CHECK_MS)
        self._fs_timer.timeout.connect(self._check_fullscreen)

        self._apply_motion("idle")

    # ---- 生命周期 ----
    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._fs_timer.start()

    def closeEvent(self, event):  # noqa: N802
        self._fs_timer.stop()
        if self._movie is not None:
            self._movie.stop()
        super().closeEvent(event)

    # ---- 动画 ----
    def _apply_motion(self, name: str) -> None:
        """切换 QMovie 动画（文件缺失回退 idle）。"""
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        path = self._motions.get(name)
        if path is None or not Path(path).exists():
            name = "idle"
            path = self._motions.get("idle")
        self._current = name
        if path is not None and Path(path).exists():
            self._movie = QMovie(str(path), parent=self)
            self._label.setMovie(self._movie)
            self._movie.setScaledSize(self._label.size())
            self._movie.start()
        else:
            self._label.clear()
            self._label.setText("🐧")
            self._label.setAlignment(Qt.AlignCenter)

    def play_motion(self, name: str) -> None:
        """外部切换动作：idle/listen/ok（缺素材回退 idle）。"""
        if name in self._motions and name != self._current:
            self._apply_motion(name)

    # ---- 鼠标拖拽 ----
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_offset = None
        self._snap_to_edge_if_near()

    # ---- 贴边隐藏（松手判定） ----
    def _snap_to_edge_if_near(self) -> None:
        scr = self.screen() or QGuiApplication.primaryScreen()
        if scr is None:
            return
        geo = scr.availableGeometry()
        pos = self.frameGeometry()
        if pos.left() <= geo.left() + _EDGE_SNAP or \
           pos.right() >= geo.right() - _EDGE_SNAP or \
           pos.top() <= geo.top() + _EDGE_SNAP:
            self._hidden_by_edge = True
            self.hide()

    # ---- 全屏检测 ----
    def _check_fullscreen(self) -> None:
        if not sys.platform.startswith("win"):
            return
        try:
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if fg == 0 or fg == int(self.winId()):
                return
            full = _is_fullscreen_window(fg)
        except Exception:
            return
        if full and self.isVisible() and not self._hidden_by_fullscreen:
            self._hidden_by_fullscreen = True
            self.hide()
        elif not full and not self.isVisible() and self._hidden_by_fullscreen:
            self._hidden_by_fullscreen = False
            self.show()

    # ---- 外部控制 ----
    def set_visible_with_pet(self, on: bool) -> None:
        """开关桌宠：显示时清除贴边/全屏隐藏态；关闭则彻底隐藏并停止动画。"""
        if on:
            self._hidden_by_edge = False
            self._hidden_by_fullscreen = False
            self.show()
        else:
            self.hide()
            self._fs_timer.stop()
