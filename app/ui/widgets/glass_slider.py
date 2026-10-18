"""纯 QPainter 自绘滑条（对标 MusicFree / 酷狗极简深色）。

GlassSlider：完全放弃原生绘制（不调用 super().paintEvent()），任何系统环境下
都不会出现原生白色底槽——底槽为 4px 半透明白细线（叠深色底 = 暗灰），已走过
部分为主题蓝，右侧透明，滑块为纯白圆点（hover 微放大）。

VolumePopover / VolumeButton：底部音量悬浮面板——平时仅一个喇叭图标，悬停/
点击在喇叭上方弹出垂直玻璃面板（底部喇叭 + 垂直 GlassSlider + 顶部百分比），
移开鼠标自动隐藏。
"""
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from ..theme.tokens import Tokens
from .icon_utils import lucide_icon


class GlassSlider(QSlider):
    """纯 QPainter 自绘滑条。支持水平 / 垂直；交互（点击 / 拖动）由 QSlider 基类
    驱动 value 变化，绘制与原生彻底解耦。"""

    def __init__(self, orientation=None, parent=None):
        # PySide6 无参 QSlider 默认 Vertical（与 C++ 不同），此处显式默认水平
        if orientation is None:
            orientation = Qt.Horizontal
        super().__init__(orientation, parent)

    def paintEvent(self, event):  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        lo, hi = self.minimum(), self.maximum()
        span = max(1, hi - lo)
        ratio = max(0.0, min(1.0, (self.value() - lo) / span))
        groove_color = QColor(255, 255, 255, 38)      # 底槽：半透明白（叠深色底=暗灰细线）
        accent = QColor(Tokens.ACCENT)
        p.setPen(Qt.NoPen)
        if self.orientation() == Qt.Horizontal:
            gx, gy, gw, gh = r.x() + 6, r.center().y() - 2, r.width() - 12, 4
            p.setBrush(groove_color)
            p.drawRoundedRect(QRectF(gx, gy, gw, gh), 2, 2)
            if ratio > 0:
                p.setBrush(accent)
                p.drawRoundedRect(QRectF(gx, gy, max(gw * ratio, 2), gh), 2, 2)
            cx, cy = gx + gw * ratio, r.center().y()
        else:
            gx, gy, gw, gh = r.center().x() - 2, r.y() + 6, 4, r.height() - 12
            p.setBrush(groove_color)
            p.drawRoundedRect(QRectF(gx, gy, gw, gh), 2, 2)
            if ratio > 0:
                fh = max(gh * ratio, 2)
                p.setBrush(accent)
                p.drawRoundedRect(QRectF(gx, gy + gh - fh, gw, fh), 2, 2)
            cx, cy = r.center().x(), gy + gh - gh * ratio
        d = 14 if self.underMouse() else 12
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(cx, cy), d / 2, d / 2)
        p.end()


class NoWheelGlassSlider(GlassSlider):
    """设置页用：滚轮悬停不改值（忽略 wheel，冒泡给 QScrollArea 滚动）。"""

    def wheelEvent(self, event):  # noqa: N802
        event.ignore()


class VolumePopover(QWidget):
    """垂直音量悬浮面板：顶部百分比 → 垂直 GlassSlider → 底部喇叭状态。
    深色半透明玻璃底（rgba(42,42,62,0.9) 圆角 + 1px 高光边框），绝无白底。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(64, 240)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        self.lbl_pct = QLabel("0%", alignment=Qt.AlignCenter)
        self.lbl_pct.setStyleSheet(
            f"color: {Tokens.TEXT_PRIMARY}; font-size: {Tokens.FONT_SIZE_SUB}px; background: transparent;")
        self.slider = GlassSlider(Qt.Vertical)
        self.slider.setRange(0, 100)
        self.slider.setFixedWidth(24)
        self.lbl_icon = QLabel(alignment=Qt.AlignCenter)
        self.lbl_icon.setStyleSheet("background: transparent;")
        lay.addWidget(self.lbl_pct)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.lbl_icon)
        self.slider.valueChanged.connect(self._sync)
        self._sync()

    def _sync(self) -> None:
        v = int(self.slider.value())
        self.lbl_pct.setText(f"{v}%")
        name = "volume-2" if v > 0 else "volume-x"   # Lucide：volume-2=有声 / volume-x=静音
        self.lbl_icon.setPixmap(
            lucide_icon(name, 18, Tokens.TEXT_PRIMARY).pixmap(18, 18))

    # ---- 弹出定位（self=面板；anchor=底部小喇叭按钮）----
    def show_near(self, anchor: QWidget) -> None:
        """在 anchor 正上方弹出。参照物显式传入，杜绝"局部坐标算全局位置"：
        X = anchor 全局中心 X - 面板宽 // 2   （面板垂直中轴线 = 喇叭图标中心线，绝对居中）
        Y = anchor 全局顶部 Y - 面板高 - 8px   （面板底部紧贴按钮上方 8px 间隙）
        mapToGlobal / move 均为 Qt 逻辑像素，同屏 DPR 一致无需换算。
        """
        btn_center = anchor.mapToGlobal(anchor.rect().center())
        btn_top = anchor.mapToGlobal(QPoint(0, 0))
        x = btn_center.x() - self.width() // 2
        y = btn_top.y() - self.height() - 8
        print(f"[Volume] btn_center=({btn_center.x()},{btn_center.y()}) "
              f"btn_top=({btn_top.x()},{btn_top.y()}) popover={self.width()}x{self.height()} "
              f"target=({x},{y})")
        self.move(x, y)
        self.show()
        self.raise_()
        # 部分平台 Popup 显示时可能调整 geometry——show 后二次校正 + 打印实际位置对照
        if self.isVisible():
            self.move(x, y)
        print(f"[Volume] after_show pos=({self.pos().x()},{self.pos().y()}) "
              f"pop_center_x={self.pos().x() + self.width() // 2} 期望={btn_center.x()}")

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        p.setBrush(QColor(42, 42, 62, 230))     # rgba(42,42,62,0.9) 玻璃底
        p.drawRoundedRect(rect, 12, 12)
        p.end()


class VolumeButton(QPushButton):
    """喇叭按钮（Lucide volume 图标）。悬停 / 点击在按钮上方弹出垂直悬浮面板，
    鼠标移开面板与按钮后自动隐藏。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("btn_status_volume")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(30, 30)
        self._popover: VolumePopover | None = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._check_mouse)
        self.clicked.connect(self._on_clicked)
        self._set_icon(0)

    # ---- 装配 ----
    def set_slider(self, slider: GlassSlider) -> None:
        """绑定外部音量滑条（业务信号仍由外部连接，这里仅做 UI 联动）。"""
        self._popover = VolumePopover()
        self._popover.slider.setRange(slider.minimum(), slider.maximum())
        self._popover.slider.setValue(slider.value())
        slider.valueChanged.connect(self._popover.slider.setValue)
        slider.valueChanged.connect(self._set_icon)
        self._popover.slider.valueChanged.connect(slider.setValue)
        # 面板自身移出即隐藏
        self._popover.installEventFilter(self)
        self._set_icon(slider.value())

    def _set_icon(self, v: int) -> None:
        name = "volume-2" if int(v) > 0 else "volume-x"   # Lucide 图标名（无裸 volume）
        self.setIcon(lucide_icon(name, 16, Tokens.TEXT_SECONDARY))

    # ---- 显隐 ----
    def _on_clicked(self) -> None:
        if self._popover is None:
            return
        if self._popover.isVisible():
            self._popover.hide()
        else:
            self._show_popover()

    def _show_popover(self) -> None:
        if self._popover is None or self._popover.isVisible():
            return
        # 定位逻辑全部在 VolumePopover.show_near：self=面板、参照物显式传入（绝对居中）
        self._popover.show_near(self)

    def enterEvent(self, event):  # noqa: N802
        self._show_popover()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._hide_timer.start(150)
        super().leaveEvent(event)

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self._popover and ev.type() in (
                ev.Type.Leave, ev.Type.Hide):
            self._hide_timer.start(150)
        return super().eventFilter(obj, ev)

    def _check_mouse(self) -> None:
        if self._popover is None or not self._popover.isVisible():
            return
        pos = self._popover.mapFromGlobal(self.cursor().pos())
        in_pop = self._popover.rect().contains(pos)
        btn = self.rect().contains(self.mapFromGlobal(self.cursor().pos()))
        if not (in_pop or btn):
            self._popover.hide()
