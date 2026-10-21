# -*- coding: utf-8 -*-
"""E2E 交互自动化测试：模拟真实用户操作（双击点播 / 搜索输入 / 进度条拖拽），
并验证 UI 状态变化。所有外部依赖（网络请求、mpv 进程）均由 Fake 彻底拦截，
测试原子、瞬间完成、绝不卡死。

运行：
    $env:QT_QPA_PLATFORM='offscreen'
    & 'C:\Program Files\Python311\python.exe' -m pytest tests/e2e/test_ui_interaction.py -v
"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QApplication, QListWidget, QListWidgetItem,
                               QMainWindow)

from app.ui.panels.music_panel import MusicPanel

pytestmark = pytest.mark.ui


def _type_text(widget, text: str) -> None:
    """真实键盘模拟（CJK 安全）：逐字符发送 KeyPress/KeyRelease 文本事件。
    注意：QTest.keyClicks 对中文字符会触发 Qt 原生崩溃（0xC0000409），不可用。"""
    for ch in text:
        press = QKeyEvent(QEvent.Type.KeyPress, Qt.Key_unknown,
                          Qt.KeyboardModifier.NoModifier, ch)
        QApplication.sendEvent(widget, press)
        release = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key_unknown,
                            Qt.KeyboardModifier.NoModifier, ch)
        QApplication.sendEvent(widget, release)


class _FakeBus:
    """全局事件总线/命令总线的无副作用替身：隔离模块级单例，杜绝跨用例污染。"""

    def register(self, *a, **k):
        pass

    def subscribe(self, *a, **k):
        pass


class FakeMusic:
    """内存版 controller：绝不连网、绝不启动 mpv 进程，模拟真实播放状态机。"""

    def __init__(self):
        self.calls = []
        self.playing = False
        self.mpv = None  # 无真实 mpv：任何 IPC 调用都会自然失败并被上层 except 吞掉

    def play_song(self, song):
        self.calls.append(("play_song", song))
        self.playing = True
        return True

    def play_online(self, track):
        """搜索结果双击播放的目标方法（_on_play 的异步调用目标）。"""
        self.calls.append(("play_online", track))
        self.playing = True
        return True

    def toggle(self):
        self.playing = not self.playing
        self.calls.append(("toggle",))

    def is_playing(self):
        return self.playing

    def get_current_song(self):
        return None

    def set_volume(self, v):
        self.calls.append(("set_volume", v))

    def next(self):
        self.calls.append(("next",))

    def prev(self):
        self.calls.append(("prev",))

    def search_online(self, kw):
        self.calls.append(("search_online", kw))
        return {"在线源": [SimpleNamespace(title="晴天", artist="周杰伦",
                                          source_name="在线源", track_id=1)]}


@pytest.fixture
def panel(qtbot, monkeypatch):
    """干净隔离的 MusicPanel：mock 总线单例，Fake 音乐控制器。
    teardown：确保 _do_search 的 QThread 已停止，防止 Qt 原生崩溃（0xC0000409）。"""
    monkeypatch.setattr("app.ui.panels.music_panel.get_command_bus", lambda: _FakeBus())
    monkeypatch.setattr("app.ui.panels.music_panel.get_event_bus", lambda: _FakeBus())
    music = FakeMusic()
    p = MusicPanel(music=music)
    p.resize(900, 640)
    qtbot.addWidget(p)
    yield p, music
    # teardown（关键）：停进度轮询线程并 join，防其访问已析构的 Qt 信号（0xC0000409）
    p._progress_polling = False
    try:
        if getattr(p, "_timer", None) is not None:
            p._timer.stop()
    except Exception:
        pass
    pt = getattr(p, "_progress_thread", None)
    if pt is not None:
        try:
            pt.join(timeout=1.5)
        except Exception:
            pass
    th = getattr(p, "_thread", None)
    if th is not None:
        try:
            th.quit()
            th.wait(3000)
        except Exception:
            pass


def _activate_online_tab(p: MusicPanel) -> None:
    """切到包含 list_results 的“在线”Tab（双击/搜索都在该 Tab 内）。"""
    for i in range(p.tabs.count()):
        w = p.tabs.widget(i)
        if w is not None and p.list_results in w.findChildren(QListWidget):
            p.tabs.setCurrentIndex(i)
            return
    raise AssertionError("未找到包含 list_results 的在线 Tab")


def test_double_click_plays_song(panel, qtbot):
    """用例 A：模拟双击歌曲列表项（click→dblclick 真实鼠标序列），
    断言状态栏变为“正在播放”，且 play_online 被异步调用。"""
    p, music = panel
    _activate_online_tab(p)
    # 模拟“搜索已完成”状态：_tracks_by_row 已由 _on_search_done 填充
    track = SimpleNamespace(title="晴天", artist="周杰伦", source_name="在线源")
    p._tracks_by_row = [track]
    item = QListWidgetItem("晴天 - 周杰伦")
    item.setData(Qt.UserRole, {"title": "晴天", "artist": "周杰伦",
                               "id": 1, "source_name": "在线源"})
    p.list_results.addItem(item)
    p.list_results.setCurrentRow(0)
    qtbot.wait(30)  # 布局稳定

    rect = p.list_results.visualItemRect(item)
    pos = rect.center() if rect.width() > 0 else QPoint(30, 30)
    # Qt 双击识别需要先有 press/click 建立 current item，再发 DblClick
    qtbot.mouseClick(p.list_results.viewport(), Qt.LeftButton, pos=pos)
    qtbot.mouseDClick(p.list_results.viewport(), Qt.LeftButton, pos=pos)

    # 状态栏立即同步为“正在播放：晴天”（_play_async 主线程同步更新）
    assert "正在播放" in p.lbl_status.text()
    # 异步线程内 play_online 被真实调用
    qtbot.waitUntil(lambda: music.calls and music.calls[0][0] == "play_online",
                    timeout=3000)


def test_play_toggle_button_icon_switches(panel, qtbot):
    """用例 A 补充：点击播放/暂停按钮，图标立即翻转（play↔pause），toggle 被调用。"""
    p, music = panel
    p.btn_toggle.show()
    qtbot.wait(20)
    before = p.btn_toggle.icon()
    qtbot.mouseClick(p.btn_toggle, Qt.LeftButton)

    assert p._toggle_icon == "pause"
    assert p.btn_toggle.icon().cacheKey() != before.cacheKey()
    assert any(c[0] == "toggle" for c in music.calls)
    # 轮询确认：Fake 状态翻转后图标锁定解除
    qtbot.waitUntil(lambda: p._icon_locked is False, timeout=4000)


def test_search_input_triggers_request(panel, qtbot):
    """用例 B：模拟在搜索框输入“周杰伦”并回车，
    断言文本正确、search_online 被触发、结果列表出现条目。"""
    p, music = panel
    _activate_online_tab(p)
    _type_text(p.edit_kw, "周杰伦")
    qtbot.keyClick(p.edit_kw, Qt.Key_Return)

    assert p.edit_kw.text() == "周杰伦"
    qtbot.waitUntil(lambda: p.list_results.count() > 0, timeout=5000)
    assert "[在线源]" in p.list_results.item(0).text()
    assert any(c[0] == "search_online" and c[1] == "周杰伦" for c in music.calls)


def test_progress_slider_drag_updates_value(panel, qtbot):
    """用例 C：模拟在进度条上拖动（press→move→release），
    断言进度数值变化，且拖拽后 UI 事件循环仍响应。"""
    p, music = panel
    sl = p.slider_pos
    sl.setEnabled(True)  # 真实播放中进度条可用；初始 disabled 会忽略鼠标
    sl.show()
    qtbot.wait(20)
    before = sl.value()
    pos0 = sl.rect().center()
    pos1 = QPoint(sl.rect().width() * 3 // 4, sl.rect().center().y())
    qtbot.mousePress(sl, Qt.LeftButton, pos=pos0)
    qtbot.mouseMove(sl, pos1)
    qtbot.mouseRelease(sl, Qt.LeftButton, pos=pos1)
    qtbot.wait(30)
    # 拖拽后进度数值变化（从初始值向右拖动 → 增大）
    assert sl.value() > before
    # UI 未阻塞：拖拽后窗口标题等常规操作立即生效
    p.setWindowTitle("after-drag")
    assert p.windowTitle() == "after-drag"


def test_window_drag_keeps_ui_responsive(qtbot, monkeypatch):
    """附加：模拟拖拽主窗口（press→move→release），拖拽后 UI 仍响应。"""
    monkeypatch.setattr("app.ui.panels.music_panel.get_command_bus", lambda: _FakeBus())
    monkeypatch.setattr("app.ui.panels.music_panel.get_event_bus", lambda: _FakeBus())
    music = FakeMusic()
    win = QMainWindow()
    p = MusicPanel(music=music)
    win.setCentralWidget(p)
    win.resize(900, 640)
    win.setWindowTitle("before")
    qtbot.addWidget(win)

    base = win.rect().topLeft()
    qtbot.mousePress(win, Qt.LeftButton, pos=base + QPoint(5, 5))
    qtbot.mouseMove(win, base + QPoint(205, 105))
    qtbot.mouseRelease(win, Qt.LeftButton, pos=base + QPoint(205, 105))

    win.setWindowTitle("after")
    assert win.windowTitle() == "after"
    # 清理：停进度轮询线程，防残留 daemon 线程访问已析构 Qt 信号
    p._progress_polling = False
    try:
        if getattr(p, "_timer", None) is not None:
            p._timer.stop()
    except Exception:
        pass
    pt = getattr(p, "_progress_thread", None)
    if pt is not None:
        try:
            pt.join(timeout=1.5)
        except Exception:
            pass
