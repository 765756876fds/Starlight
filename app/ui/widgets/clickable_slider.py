"""共享可点击进度条：ClickableSlider(QSlider)。

支持"点击 groove 任意位置精确跳转"，同时保留基类拖拽能力：
- 命中 handle（±10px）→ 交还基类（正常拖拽，release 由基类 emit sliderReleased）；
- 点击 groove → 按点击 x/宽度 计算精确 value → setValue + 手动 emit
  sliderReleased（触发调用方后台 seek）——不调 super 的 mousePress，
  避免基类 PageStep 步进覆盖精确值；release 也不调 super，防重复 emit。

使用方（电影面板 / 视频播放器 overlay）统一 import 本类。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSlider


class ClickableSlider(QSlider):
    """支持"点击任意位置直接跳转"的 QSlider：点击 groove 精确 setValue 并在
    mouseRelease 时手动发 sliderReleased（触发调用方后台 seek）；
    命中 handle 时交还基类拖拽逻辑（release 由基类 emit，防重复触发）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._clicked_to_seek = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._hit_handle(event.position().x()):
            w = self.width()
            if w > 0:
                x = event.position().x()
                frac = max(0.0, min(1.0, x / w))
                val = self.minimum() + int(frac * (self.maximum() - self.minimum()))
                self.setValue(val)
                self.update()
                # 不调 super：基类对 groove 的 PageStep 步进会覆盖精确值
                self._clicked_to_seek = True
                event.accept()
                return
        self._clicked_to_seek = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._clicked_to_seek:
            self._clicked_to_seek = False
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _hit_handle(self, x: float) -> bool:
        rng = self.maximum() - self.minimum()
        if rng <= 0:
            return True
        ratio = (self.value() - self.minimum()) / rng
        pad = 6.0
        hx = pad + (self.width() - pad * 2) * ratio
        return abs(x - hx) <= 10.0
