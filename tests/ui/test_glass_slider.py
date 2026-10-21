"""
GlassSlider / 音量悬浮面板（MusicFree 极简风）测试：
- GlassSlider 纯 QPainter 绘制（水平/垂直）：左/下亮蓝、右/上透明露深灰底槽、无白底
- PySide6 无参 QSlider 默认 Vertical → GlassSlider 显式默认 Horizontal
- VolumePopover 玻璃底 + 百分比联动
- VolumeButton 装配联动（slider ↔ popover ↔ 喇叭图标）
- 设置页 NoWheelSlider 继承 GlassSlider；MusicPanel 音量区使用新组件
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytestmark = pytest.mark.ui

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from app.ui.theme.applier import apply_glass_theme
from app.ui.widgets.glass_slider import GlassSlider, VolumeButton, VolumePopover


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    apply_glass_theme(app)
    yield app


def _grab_on_dark(qapp, slider, size=(300, 40)):
    """深色宿主上截图（模拟真实深色窗口，避免 grab 白底合成误导）。"""
    host = QWidget()
    host.setStyleSheet("background: #1e1e2e;")
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    slider.setFixedSize(*size)
    lay.addWidget(slider)
    host.resize(*size)
    host.show()
    qapp.processEvents()
    img = host.grab().toImage()
    host.close()
    return img


def test_glass_slider_default_horizontal(qapp):
    """PySide6 无参 QSlider 默认 Vertical（与 C++ 不同），GlassSlider 必须显式默认水平。"""
    s = GlassSlider()
    assert s.orientation() == Qt.Horizontal


def test_glass_slider_horizontal_left_blue_right_dark(qapp):
    """水平：value=70 → 左端亮蓝，右端底槽深灰（无白底）。"""
    s = GlassSlider()
    s.setRange(0, 100)
    s.setValue(70)
    img = _grab_on_dark(qapp, s)
    w, h = img.width(), img.height()
    cy = int(h * 0.5)
    left = img.pixelColor(int(w * 0.15), cy)
    right = img.pixelColor(int(w * 0.85), cy)
    assert left.blue() > 120 and left.blue() > left.red(), f"左侧应亮蓝, got {left.name()}"
    assert right.lightness() < 110 and right.lightness() > 25, \
        f"右侧应为深灰底槽（非白）, got {right.name()}"
    assert right.name() != "#ffffff", "绝对不允许白底"


def test_glass_slider_vertical_bottom_blue_top_dark(qapp):
    """垂直：value=70 → 下端亮蓝（从下往上填充），上端透明露深灰底槽。"""
    s = GlassSlider(Qt.Vertical)
    s.setRange(0, 100)
    s.setValue(70)
    img = _grab_on_dark(qapp, s, size=(40, 300))
    w, h = img.width(), img.height()
    cx = int(w * 0.5)
    bottom = img.pixelColor(cx, int(h * 0.85))
    top = img.pixelColor(cx, int(h * 0.15))
    assert bottom.blue() > 120 and bottom.blue() > bottom.red(), f"下端应亮蓝, got {bottom.name()}"
    assert top.lightness() < 110 and top.name() != "#ffffff", f"上端应深灰底槽, got {top.name()}"


def test_volume_popover_glass_background_and_pct(qapp):
    """悬浮面板：玻璃深底（rgba(42,42,62,230) ≈ #2a2a3e，无白底），滑块改值 → 百分比联动。"""
    p = VolumePopover()
    p.show()
    qapp.processEvents()
    img = p.grab().toImage()
    center = img.pixelColor(int(img.width() * 0.5), int(img.height() * 0.5))
    assert center.lightness() < 90, f"面板中心应为玻璃深底, got {center.name()}"
    assert center.name() != "#ffffff"
    p.slider.setValue(14)
    qapp.processEvents()
    assert p.lbl_pct.text() == "14%"
    p.hide()
    p.deleteLater()
    qapp.processEvents()


def test_volume_button_links_slider_popover_icon(qapp):
    """装配：外部 slider ↔ popover.slider 双向同步；喇叭图标随音量状态切换（volume/volume-x）。"""
    s = GlassSlider(Qt.Vertical)
    s.setRange(0, 100)
    s.setValue(0)
    btn = VolumeButton()
    btn.set_slider(s)
    try:
        assert btn._popover is not None
        assert btn._popover.slider.value() == 0
        # 外部改音量 → popover 与喇叭图标跟随
        s.setValue(42)
        qapp.processEvents()
        assert btn._popover.slider.value() == 42
        assert btn._popover.lbl_pct.text() == "42%"
        # 喇叭图标随音量状态切换（volume-2 / volume-x 图标缓存不同）
        k_vol = btn.icon().cacheKey()
        s.setValue(0)
        qapp.processEvents()
        k_mute = btn.icon().cacheKey()
        assert btn._popover.lbl_pct.text() == "0%"
        assert k_mute != k_vol, "静音状态喇叭图标应切换为 volume-x"
        # 面板显隐：点击按钮切换
        btn._show_popover()
        assert btn._popover.isVisible()
        btn._popover.hide()
    finally:
        btn._popover.deleteLater()
        btn.deleteLater()
        qapp.processEvents()


def test_settings_nowheel_slider_is_glass(qapp):
    """设置页 NoWheelSlider 必须继承 GlassSlider（纯 QPainter，放弃原生绘制）。"""
    from app.ui.panels.settings_panel import NoWheelSlider
    assert issubclass(NoWheelSlider, GlassSlider)


def test_volume_popover_position_aligned(qapp):
    """悬浮面板弹出位置：X = 按钮全局中心 X - 面板宽/2（左右居中）；Y = 按钮全局顶部
    Y - 面板高 - 8px 间隙（紧贴上方）。mapToGlobal 修正父窗口 padding。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QHBoxLayout, QWidget

    host = QWidget()
    lay = QHBoxLayout(host)
    lay.setContentsMargins(50, 30, 50, 30)   # 父容器 padding：mapToGlobal 必须正确扣除
    s = GlassSlider(Qt.Vertical)
    s.setRange(0, 100)
    btn = VolumeButton()
    btn.set_slider(s)
    lay.addWidget(btn)
    host.resize(160, 120)
    host.show()
    qapp.processEvents()
    btn._show_popover()
    qapp.processEvents()
    p = btn._popover
    assert p.isVisible()
    expect_x = btn.mapToGlobal(btn.rect().center()).x() - p.width() // 2
    expect_y = btn.mapToGlobal(QPoint(0, 0)).y() - p.height() - 8
    assert abs(p.x() - expect_x) <= 1, f"面板 X 应居中对齐按钮, got {p.x()} expect {expect_x}"
    assert abs(p.y() - expect_y) <= 1, f"面板 Y 应紧贴按钮上方, got {p.y()} expect {expect_y}"
    p.hide()


def test_music_panel_volume_uses_glass_ui(qapp):
    """MusicPanel 音量区：slider_vol 为垂直 GlassSlider，btn_volume 已装配悬浮面板，
    业务信号（valueChanged/sliderReleased）连接保持不变。"""
    from unittest.mock import MagicMock
    from app.ui.panels.music_panel import MusicPanel

    class FakeMusic:
        def get_play_mode(self):
            return "sequential"

        def is_highlight_mode(self):
            return False

        def get_volume(self):
            return 66

        def set_volume(self, v):
            self._volume = v

        def get_online_sources(self):
            return []

        def search_local(self, kw=""):
            return []

        def get_current_song(self):
            return None

    panel = MusicPanel(music=FakeMusic())
    try:
        assert isinstance(panel.slider_vol, GlassSlider), "音量滑条必须为 GlassSlider"
        assert panel.slider_vol.orientation() == Qt.Vertical, "悬浮面板内为垂直滑条"
        assert isinstance(panel.btn_volume, VolumeButton)
        assert panel.btn_volume._popover is not None, "喇叭按钮需装配悬浮面板"
        # 业务信号仍在（valueChanged → 业务槽）
        assert hasattr(panel, "_on_volume")
        assert hasattr(panel, "_on_volume_released")
        # 初始音量同步到悬浮面板
        assert panel.btn_volume._popover.slider.value() == 66
    finally:
        panel._timer.stop()
