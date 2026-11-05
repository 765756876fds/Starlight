"""律动波形进度条（酷狗风格）：QTimer 60ms 更新频谱缓冲（平滑随机漫步+正弦包络），
paintEvent 用 QPainter 自绘——波形在进度条上方，已播放区亮蓝、未播放半透明，
handle 纯白圆点带微光。播放中跳动、暂停时降到低电平，CPU 占用极低。

性能红线：不在 paintEvent 做 IO/分配大对象；频谱为纯数学模拟。
"""
from __future__ import annotations

import math
import random

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QSlider

from ..theme.colors import Colors
from ..theme.tokens import Tokens

_BAR_COUNT = 30
_TICK_MS = 60


class WaveformSlider(QSlider):
    """进度条 + 顶部律动频谱。value/minimum/maximum 与普通 QSlider 一致。"""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, 1000)
        self._bars = [0.05] * _BAR_COUNT
        self._tick = 0
        self._active = False            # 播放中才跳动
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._step)
        self._clicked_to_seek = False   # 点击 groove 精确跳转标志（release 时手动发 seek）
        # 不在此 start：生命周期完全交给 showEvent/hideEvent（隐藏时停、显示时启，防空转）

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.stop()

    def set_active(self, on: bool) -> None:
        self._active = bool(on)
        if not self._active:
            self.update()

    # ---- 频谱模拟（平滑随机漫步 + 正弦包络；播放中振幅大、暂停低电平） ----
    def _step(self) -> None:
        self._tick += 1
        amp = 1.0 if self._active else 0.12
        phase = self._tick * 0.05
        for i in range(_BAR_COUNT):
            env = 0.30 + 0.70 * abs(math.sin(phase + i * 0.32))
            t = env * (0.55 + 0.45 * random.random()) * amp
            self._bars[i] = self._bars[i] * 0.70 + t * 0.30
        self.update()

    # ---- 自绘：groove + 已播放渐变 + 波形柱 + 白点 handle ----
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        ratio = 0.0
        rng = self.maximum() - self.minimum()
        if rng > 0:
            ratio = (self.value() - self.minimum()) / rng
        ratio = max(0.0, min(1.0, ratio))

        cx = self.height() / 2.0
        pad = 6.0
        groove_h = 4.0
        gw = w - pad * 2.0
        fill_w = gw * ratio

        # ① groove 底座（半透明深灰，与全局 QSlider groove 同款）
        _rgba = [int(x) for x in str(Tokens.TEXT_RGB).split(",")]
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_rgba[0], _rgba[1], _rgba[2], 45))
        p.drawRoundedRect(QRectF(pad, cx - groove_h / 2, gw, groove_h),
                          groove_h / 2, groove_h / 2)
        # ② 已播放区（亮蓝渐变）
        if fill_w > 0:
            grad = QLinearGradient(pad, 0, pad + fill_w, 0)
            grad.setColorAt(0.0, QColor(Colors.ACCENT))
            grad.setColorAt(1.0, QColor(Colors.ACCENT_HOVER))
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(pad, cx - groove_h / 2, fill_w, groove_h),
                              groove_h / 2, groove_h / 2)

        # ③ 波形柱：进度条上方（groove 顶到 22px 高区域）
        bar_w = 3.0
        gap = 2.0
        total = bar_w + gap
        start_x = pad + 4.0
        wave_bottom = cx - groove_h - 4.0
        wave_top = wave_bottom - 22.0
        for i, v in enumerate(self._bars):
            x = start_x + i * total
            if x + bar_w > w - pad:
                break
            h = max(2.0, (wave_bottom - wave_top) * min(1.0, v))
            played = (x + bar_w / 2) <= fill_w
            if played:
                p.setBrush(QColor(Colors.ACCENT_HOVER))
            else:
                color = QColor(Tokens.WAVEFORM_COLOR)
                color.setAlpha(Tokens.WAVEFORM_ALPHA)
                p.setBrush(color)
            p.drawRoundedRect(QRectF(x, wave_bottom - h, bar_w, h),
                              bar_w / 2, bar_w / 2)

        # ④ handle：纯白圆点 + accent 微光
        hx = pad + fill_w
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(Colors.ACCENT))
        p.drawEllipse(QRectF(hx - 8, cx - 8, 16, 16))
        p.setBrush(QColor(Tokens.SLIDER_HANDLE))
        p.drawEllipse(QRectF(hx - 5.5, cx - 5.5, 11, 11))

        # ⑤ 阴影微光（可忽略渲染开销：一次径向渐变）
        p.setBrush(QColor(Colors.ACCENT_HOVER))
        p.drawEllipse(QRectF(hx - 10, cx - 10, 2, 2))
        p.end()

    # ---- 鼠标：点击 groove 精确跳转；命中 handle 时交还基类拖拽 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._hit_handle(event.position().x()):
            w = self.width()
            if w > 0:
                x = event.position().x()
                frac = max(0.0, min(1.0, x / w))
                val = self.minimum() + int(frac * (self.maximum() - self.minimum()))
                self.setValue(val)
                self.sliderMoved.emit(val)  # 通知预览（_on_seek_preview 只改时间标签）
                self.update()
                # 不调用 super().mousePressEvent：基类对 groove 的 PageStep 步进
                # 会覆盖精确值；拖拽状态（handle 按下）由下方 super 路径正常建立。
                self._clicked_to_seek = True
                event.accept()
                return
        self._clicked_to_seek = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        # 点击路径：手动发 sliderReleased 触发真正 seek（后台线程），
        # 不调 super 防基类重复 emit；拖拽路径由基类 release 正常 emit。
        if event.button() == Qt.LeftButton and self._clicked_to_seek:
            self._clicked_to_seek = False
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _hit_handle(self, x: float) -> bool:
        """按下点是否命中 handle（半径 10px，与自绘 handle 尺寸一致）。"""
        rng = self.maximum() - self.minimum()
        if rng <= 0:
            return True
        ratio = (self.value() - self.minimum()) / rng
        pad = 6.0
        hx = pad + (self.width() - pad * 2) * ratio
        return abs(x - hx) <= 10.0
