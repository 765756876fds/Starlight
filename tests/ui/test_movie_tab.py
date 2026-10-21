"""
电影 tab 挂载验收：生产路径容器 MainTabs 必须含"电影"tab。

历史坑：上批挂载在 MainWindow.switch_to_tabs(tabs=None) 的内部 build 分支，
但 main.py 生产路径永远传外部 MainTabs 对象（switch_to_tabs(tabs) 走
tabs is not None 分支），内部 build 永不执行 → 电影 tab 实际不显示。
本测试直接对生产路径容器断言。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytestmark = pytest.mark.ui

from PySide6.QtWidgets import QApplication, QTabWidget

from app.ui.panels.movie_panel import MoviePanel
from app.ui.widgets.main_tabs import MainTabs


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _stop_timers(mt):
    for p in getattr(mt, "_panels", {}).values():
        t = getattr(p, "_timer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass


def test_main_tabs_contains_movie_tab(qapp):
    """生产路径容器 tab 名含"电影"，且顺序在音乐之后、设置之前。"""
    mt = MainTabs(automation_service=None, cooldown_manager=None, music=None,
                  ongoing_service=None, services_dict=None, combo_manager=None, tts=None)
    try:
        tw = mt.findChild(QTabWidget)
        assert tw is not None
        names = [tw.tabText(i) for i in range(tw.count())]
        assert "电影" in names, f"电影 tab 缺失，当前 tab: {names}"
        assert names.index("音乐") < names.index("电影") < names.index("设置"), \
            f"电影 tab 顺序错，当前 tab: {names}"
    finally:
        _stop_timers(mt)


def test_movie_tab_is_movie_panel(qapp):
    """电影 tab 实例是 MoviePanel（构造不异常，含自建 controller）。"""
    mt = MainTabs(automation_service=None, cooldown_manager=None, music=None,
                  ongoing_service=None, services_dict=None, combo_manager=None, tts=None)
    try:
        panel = mt._panels.get("movie")
        assert isinstance(panel, MoviePanel)
        assert panel._movie is not None   # movie=None → 面板自建 controller
    finally:
        _stop_timers(mt)


def test_main_window_internal_build_has_no_duplicate_movie(qapp):
    """MainWindow 内部 build 分支死挂载已删：其 tab 不应再含电影（避免双挂/误导）。"""
    from app.ui.main_window import MainWindow
    w = MainWindow(music=None)
    w.switch_to_tabs(None)
    try:
        tw = w.findChild(QTabWidget)
        assert tw is not None
        names = [tw.tabText(i) for i in range(tw.count())]
        assert "电影" not in names, f"死分支仍残留电影挂载: {names}"
    finally:
        _stop_timers(w)
