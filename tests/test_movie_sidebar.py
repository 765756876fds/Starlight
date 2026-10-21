"""电影内嵌 S4：选集侧栏测试（offscreen）。

覆盖：独立顶层标志 / 单集隐藏（电影无选集）/ 多集渲染 + 当前集高亮 /
右缘鼠标触发浮现 / 3s 静止隐藏 / hover 不隐藏 / 点集数切集（复用 play 链）/
定位跟随窗口 resize。
"""
import pytest

pytestmark = pytest.mark.ui

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])


class _FakeCtl:
    """S4 测试 mock 控制器（记录 play 调用）。"""

    def __init__(self):
        self.plays = []          # (item, index)
        self.wids = []
        self.playing = True

    def set_embed_wid(self, wid):
        self.wids.append(wid)

    def play(self, item, index=None):
        self.plays.append((item, index))
        return True

    def is_playing(self):
        return self.playing

    def stop(self):
        self.playing = False

    def tick(self):
        pass

    def get_position(self):
        return 0.0

    def get_duration(self):
        return 0.0

    def current_item(self):
        return None

    def is_auto_next(self):
        return True

    def set_auto_next(self, enabled):
        pass


def _mk_window():
    from app.ui.widgets.video_player_window import VideoPlayerWindow
    ctl = _FakeCtl()
    w = VideoPlayerWindow(ctl)
    return w, ctl


def _flush(qapp):
    for _ in range(3):
        qapp.processEvents()


def _episode_item(play_url, count):
    """构造分集 play_url（名字$URL#...）。"""
    segs = "".join(f"第{i}集$https://x/{i}.m3u8#" for i in range(1, count + 1))
    return {"vod_name": "测试剧", "play_url": segs.rstrip("#")}


class TestEpisodeSidebar:
    def test_sidebar_flags(self):
        """侧栏独立顶层：Qt.Tool | Frameless，owner = 播放窗口（S7 去永久置顶——
        被盖跟随播放窗口，但仍浮在 mpv 之上，同 overlay 模式）。"""
        w, _ = _mk_window()
        try:
            sb = w.sidebar
            assert sb.windowFlags() & Qt.Tool
            assert sb.windowFlags() & Qt.FramelessWindowHint
            assert not (sb.windowFlags() & Qt.WindowStaysOnTopHint)
            assert sb.parent() is w
        finally:
            w._timer.stop()

    def test_single_episode_hidden(self):
        """单集（电影）→ 侧栏隐藏（无选集不显示）。"""
        w, _ = _mk_window()
        try:
            w.sidebar.set_episodes([("第1集", "https://x/1.m3u8")])
            assert w.sidebar.episode_count() == 1
            assert w.sidebar.isVisible() is False
        finally:
            w._timer.stop()

    def test_multi_episode_renders_and_highlight(self):
        """多集 → 列表渲染 + 当前集高亮（第 2 集）。"""
        w, _ = _mk_window()
        try:
            eps = [("第1集", "u1"), ("第2集", "u2"), ("第3集", "u3")]
            w.sidebar.set_episodes(eps, current=1)
            assert w.sidebar.episode_count() == 3
            assert w.sidebar._list.count() == 3
            assert w.sidebar._list.currentRow() == 1          # 当前集高亮
            assert w.sidebar._list.item(0).text() == "第1集"
            assert w.sidebar._list.item(2).text() == "第3集"
        finally:
            w._timer.stop()

    def test_edge_mouse_shows_sidebar(self):
        """右缘鼠标（x ≥ 宽-60）→ 侧栏滑出 + 重置 3s 隐藏计时。"""
        w, _ = _mk_window()
        try:
            w.sidebar.set_episodes([("第1集", "u1"), ("第2集", "u2")], current=0)
            w._playing = True
            w._show_sidebar()
            assert w.sidebar.isVisible() is True
            assert w._sidebar_hide_timer.isActive() is True
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_center_mouse_keeps_sidebar_hidden(self):
        """中间区域鼠标 → 侧栏不浮现（只右缘触发）。"""
        w, _ = _mk_window()
        try:
            w.sidebar.set_episodes([("第1集", "u1"), ("第2集", "u2")], current=0)
            w._playing = True
            w.notify_mouse_position(10)                      # x 远小于 宽-60
            assert w.sidebar.isVisible() is False
            w.notify_mouse_position(w.width() - 10)          # 右缘
            assert w.sidebar.isVisible() is True
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_hide_after_timeout(self):
        """3s 无活动 → 侧栏隐藏。"""
        w, _ = _mk_window()
        try:
            w.sidebar.set_episodes([("第1集", "u1"), ("第2集", "u2")], current=0)
            w._playing = True
            w._show_sidebar()
            assert w.sidebar.isVisible() is True
            w._on_sidebar_hide_timeout()
            assert w.sidebar.isVisible() is False
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_hover_keeps_visible(self):
        """侧栏 hover → 不隐藏（停计时）；离开 → 重新计时。"""
        w, _ = _mk_window()
        try:
            w.sidebar.set_episodes([("第1集", "u1"), ("第2集", "u2")], current=0)
            w._playing = True
            w._show_sidebar()
            w._on_sidebar_hover(True)
            assert w._sidebar_hide_timer.isActive() is False
            w._on_sidebar_hide_timeout()
            assert w.sidebar.isVisible() is True              # hover 中不隐藏
            w._on_sidebar_hover(False)
            assert w._sidebar_hide_timer.isActive() is True   # 离开重新计时
            w._on_sidebar_hide_timeout()
            assert w.sidebar.isVisible() is False
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_click_switches_episode(self):
        """点集数 → 复用 play 链切集（item + index 传对）+ 高亮更新。"""
        from PySide6.QtWidgets import QListWidgetItem
        w, ctl = _mk_window()
        try:
            item = _episode_item("x", 5)
            w.play(item, index=1)                             # 播第 2 集
            assert w._current_item is item
            assert ctl.plays[-1] == (item, 1)
            # 模拟点击第 4 集（row 3）
            w.sidebar._on_click(w.sidebar._list.item(3))
            assert ctl.plays[-1] == (item, 3)                  # play(item, index=3)
            assert w.sidebar._list.currentRow() == 3           # 高亮更新
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_position_follows_window(self):
        """窗口 resize → 侧栏贴右缘（x = 右缘 - 150；底部让开 overlay）。"""
        w, _ = _mk_window()
        try:
            w.sidebar.set_episodes([("第1集", "u1"), ("第2集", "u2")], current=0)
            w._playing = True
            w._show_sidebar()
            w.resize(1200, 700)
            w._position_sidebar()
            assert w.sidebar.x() == 1200 - 150
            assert w.sidebar.y() == 0
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_play_sets_episodes_and_highlight(self):
        """play 时解析 play_url → 侧栏剧集 + 当前集高亮。"""
        w, _ = _mk_window()
        try:
            item = _episode_item("x", 4)
            w.play(item, index=2)                              # 播第 3 集
            assert w.sidebar.episode_count() == 4
            assert w.sidebar._list.currentRow() == 2
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_play_single_movie_hides_sidebar(self):
        """play 单集（电影）→ 侧栏隐藏（无选集）。"""
        w, _ = _mk_window()
        try:
            item = {"vod_name": "电影", "play_url": "第1集$https://x/m.m3u8"}
            w.play(item, index=0)
            assert w.sidebar.episode_count() == 1
            assert w.sidebar.isVisible() is False
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()

    def test_stopped_hides_sidebar(self):
        """停播 → 侧栏隐藏 + 计时器停 + 当前 item 清。"""
        w, _ = _mk_window()
        try:
            item = _episode_item("x", 3)
            w.play(item, index=0)
            w._show_sidebar()
            assert w.sidebar.isVisible() is True
            w._on_stopped()
            assert w.sidebar.isVisible() is False
            assert w._current_item is None
            assert w._sidebar_hide_timer.isActive() is False
        finally:
            w._timer.stop()
            w._sidebar_hide_timer.stop()
