"""设置页方案 B：4 tab 结构回归测试。

- 4 个 tab（音乐/语音/游戏/界面），标题不带 emoji
- 每个 tab 页是 QScrollArea（独立滚动）
- 插件区不再内嵌滚动（平铺，_plugins_container 直接挂组 layout）
- 构造后关键控件属性仍存在（防重设计误删控件）
"""
import os
import sys

import pytest
from PySide6.QtWidgets import QApplication, QGroupBox, QScrollArea

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ui.panels.settings_panel import SettingsPanel  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    return a


def _make_panel(app):
    from unittest.mock import MagicMock
    music = MagicMock()
    music.online_manager = None  # 无在线管理器 → 插件区走"JS 桥接未启用"分支
    return SettingsPanel(
        music=music,
        tts=MagicMock(),
        combo_manager=MagicMock(),
        equip_toggle=lambda on: None,
        on_refresh_changed=lambda ms: None,
        overlay_toggle=lambda on: None,
        overlay_enabled=True,
        parent=None,
    )


def test_four_tabs_in_order(app):
    p = _make_panel(app)
    assert p._tabs.count() == 4
    assert [p._tabs.tabText(i) for i in range(p._tabs.count())] == \
        ["音乐", "语音", "游戏", "界面"]


def test_each_tab_page_is_scrollarea(app):
    p = _make_panel(app)
    for i in range(p._tabs.count()):
        w = p._tabs.widget(i)
        assert isinstance(w, QScrollArea)
        assert w.widgetResizable() is True


def test_plugins_flat_no_inner_scroll(app):
    """插件行容器直接挂组 layout（父级是 QGroupBox），不再包内嵌 QScrollArea。"""
    p = _make_panel(app)
    assert isinstance(p._plugins_container.parent(), QGroupBox)


def test_key_widgets_alive_after_redesign(app):
    """构造后关键控件属性仍存在（重设计防误删）。"""
    p = _make_panel(app)
    for attr in (
        "cmb_engine", "cmb_voice", "_rate_slider", "_vol_slider",
        "_key_edit", "_btn_key_save",
        "btn_add_dir", "btn_scan_all", "btn_source_manage",
        "btn_refresh_plugins", "btn_check_updates",
        "btn_gate", "btn_equip", "btn_update_data",
        "btn_overlay", "spin_refresh",
    ):
        assert getattr(p, attr) is not None, f"控件缺失: {attr}"
