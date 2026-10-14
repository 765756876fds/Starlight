"""设置页 NoWheelSlider：滚轮悬停不改值（事件 ignore → 冒泡给父滚动区）。

对照组：普通 QSlider 默认 accept 消费 wheel 事件（保持平台默认行为不受影响）。
"""
import os
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QSlider

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ui.panels.settings_panel import NoWheelSlider  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    return a


class FakeWheelEvent:
    def __init__(self):
        self.ignored = False
        self.accepted = False

    def ignore(self):
        self.ignored = True

    def accept(self):
        self.accepted = True


def test_nowheel_slider_wheel_ignored(app):
    s = NoWheelSlider(Qt.Horizontal)
    s.setValue(50)
    e = FakeWheelEvent()
    s.wheelEvent(e)
    assert e.ignored is True  # 事件被忽略 → 冒泡给滚动区
    assert s.value() == 50    # 值未被滚轮改变


def test_nowheel_slider_still_settable(app):
    """拖拽/程序化改值不受影响（wheelEvent 只拦滚轮）。"""
    s = NoWheelSlider(Qt.Horizontal)
    s.setRange(0, 100)
    s.setValue(20)
    s.setValue(75)
    assert s.value() == 75


def test_plain_slider_still_default(app):
    """对照组：普通 QSlider 默认消费 wheel 事件（改值），行为不受影响。"""
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    s = QSlider(Qt.Horizontal)
    s.setRange(0, 100)
    s.setValue(50)
    e = QWheelEvent(
        QPointF(10, 10), QPointF(10, 10),
        QPoint(0, 0), QPoint(0, 120),  # angleDelta +120 = 滚轮上滚
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    s.wheelEvent(e)
    assert s.value() != 50  # 默认 QSlider 被滚轮改值
