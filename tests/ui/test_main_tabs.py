import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import ExitStack
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QWidget

_app = QApplication.instance() or QApplication([])


def test_main_tabs_groups_game_pages_without_rebuilding_them():
    from app.ui.widgets import main_tabs as module

    classes = [
        "CurrentGamePanel", "AutomationPanel", "OngoingPanel",
        "MusicPanel", "MoviePanel", "ConsolePanel", "SettingsPanel",
    ]
    patches = [patch.object(module, name, QWidget) for name in classes]
    with ExitStack() as stack:
        stack.enter_context(patch.object(module, "load_ui_prefs", return_value={}))
        for p in patches:
            stack.enter_context(p)
        tabs_root = module.MainTabs(None, None, None)

    top_tabs = tabs_root.layout().itemAt(0).widget()
    assert top_tabs.count() == 5
    assert [top_tabs.tabText(i) for i in range(top_tabs.count())] == [
        "游戏", "音乐", "电影", "通知中心", "设置"
    ]
    game = top_tabs.widget(0)
    assert game.tabs.count() == 3
    assert [game.tabs.tabText(i) for i in range(game.tabs.count())] == [
        "当前对局", "自动化", "对局信息"
    ]
    assert game.tabs.currentIndex() == 0
