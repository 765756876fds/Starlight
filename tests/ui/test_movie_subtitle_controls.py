"""真实 Qt 信号与事件循环；播放器/文件选择隔离，不打开私人媒体。"""
import copy
import threading
import time

import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.ui.widgets.video_player_window import VideoPlayerWindow


_app = QApplication.instance() or QApplication([])


def wait_for(condition):
    deadline = time.monotonic() + 4
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert condition()


class Movie:
    def __init__(self):
        self.state = {"media": "test.mp4", "visible": True, "sid": 1,
                      "tracks": [{"id": 1, "title": "中文", "lang": "zh"},
                                 {"id": 2, "title": "English", "lang": "en"}]}
        self.calls = []
        self.query_threads = []
        self.gate = None
        self.toggles = 0

    def subtitle_state(self):
        self.query_threads.append(threading.get_ident())
        state = copy.deepcopy(self.state)
        if self.gate is not None:
            assert self.gate.wait(3)
        return state

    def load_local_subtitle(self, path, expected_media=None):
        self.calls.append(("load", path, expected_media))
        return True, path

    def select_subtitle(self, sid, expected_media=None):
        self.calls.append(("select", sid, expected_media))
        self.state["sid"] = sid
        self.state["visible"] = True
        return True, "字幕已开启"

    def set_subtitle_visible(self, visible, expected_media=None):
        self.calls.append(("visible", visible, expected_media))
        self.state["visible"] = visible
        return True, "字幕状态已更新"

    def set_embed_wid(self, wid):
        pass

    def play(self, item, index=None):
        self.state = {"media": item["first_url"], "tracks": [], "visible": True, "sid": False}
        return True

    def is_playing(self):
        return True

    def stop(self):
        pass

    def toggle_pause(self):
        self.toggles += 1
        return True


@pytest.fixture
def player(monkeypatch):
    movie = Movie()
    w = VideoPlayerWindow(movie)
    w._playing = True
    w._current_item = {"vod_name": "测试电影"}
    monkeypatch.setattr(w, "_apply_mpv_mouse_passthrough", lambda: None)
    feedback = []
    monkeypatch.setattr(w, "_subtitle_feedback", lambda text: feedback.append((text, QThread.currentThread())))
    yield w, movie, feedback
    if movie.gate is not None:
        movie.gate.set()
    wait_for(lambda: not w._subtitle_busy)
    w._timer.stop()
    w.close()


def open_menu(w):
    QTest.mouseClick(w.overlay.btn_subtitle, Qt.LeftButton)
    wait_for(lambda: not w._subtitle_busy and w._subtitle_menu is not None)
    return w._subtitle_menu


def action(menu, text):
    return next(a for a in menu.actions() if text in a.text())


def test_button_queries_off_gui_thread_and_builds_menu_on_gui_thread(player, monkeypatch):
    w, movie, _ = player
    gui_calls = []
    original = w._build_subtitle_menu

    def build(state):
        gui_calls.append(QThread.currentThread())
        return original(state)

    monkeypatch.setattr(w, "_build_subtitle_menu", build)
    menu = open_menu(w)
    assert movie.query_threads == [movie.query_threads[0]]
    assert movie.query_threads[0] != threading.get_ident()
    assert gui_calls == [_app.thread()]
    assert action(menu, "加载本地字幕").isEnabled()
    assert action(menu, "中文").isChecked()
    assert not action(menu, "English").isChecked()
    assert movie.toggles == 0


def test_cancel_file_picker_sends_no_load_or_state_change(player, monkeypatch):
    w, movie, feedback = player
    original = copy.deepcopy(movie.state)
    monkeypatch.setattr("app.ui.widgets.video_player_window.QFileDialog.getOpenFileName",
                        lambda *a, **kw: ("", ""))
    action(open_menu(w), "加载本地字幕").trigger()
    QTest.qWait(20)
    assert movie.calls == [] and movie.state == original and feedback == []


def test_local_picker_routes_to_controller_and_feedback_is_on_gui_thread(player, monkeypatch, tmp_path):
    w, movie, feedback = player
    subtitle = str(tmp_path / "中文 字幕.srt")
    monkeypatch.setattr("app.ui.widgets.video_player_window.QFileDialog.getOpenFileName",
                        lambda *a, **kw: (subtitle, ""))
    action(open_menu(w), "加载本地字幕").trigger()
    wait_for(lambda: len(feedback) == 1)
    assert movie.calls == [("load", subtitle, "test.mp4")]
    assert "字幕已加载" in feedback[0][0]
    assert feedback[0][1] == _app.thread()


def test_visibility_and_track_actions_use_actual_menu_media(player):
    w, movie, feedback = player
    action(open_menu(w), "显示字幕").trigger()
    wait_for(lambda: len(feedback) == 1)
    assert movie.calls[-1] == ("visible", False, "test.mp4")
    action(open_menu(w), "English").trigger()
    wait_for(lambda: len(feedback) == 2)
    assert movie.calls[-1] == ("select", 2, "test.mp4")
    menu = open_menu(w)
    assert action(menu, "English").isChecked()
    assert action(menu, "显示字幕").isChecked()


def test_assrt_dialog_entry_remains_available(player, monkeypatch):
    from app.ui.panels import movie_panel
    w, movie, _ = player
    calls = []

    class Dialog:
        def __init__(self, controller, keyword, parent):
            calls.append((controller, keyword, parent))

        def exec(self):
            calls.append("exec")

    monkeypatch.setattr(movie_panel, "_SubtitleDialog", Dialog)
    action(open_menu(w), "Assrt").trigger()
    assert calls == [(movie, "测试电影", w), "exec"]


def test_late_menu_snapshot_is_discarded_after_switching_movie(player):
    w, movie, feedback = player
    movie.gate = threading.Event()
    w.request_subtitle()
    wait_for(lambda: bool(movie.query_threads))
    w.request_subtitle()  # 忙时不再创建第二个查询任务。
    assert len(movie.query_threads) == 1
    assert w.play({"vod_name": "新电影", "first_url": "new.mp4"}) is True
    movie.gate.set()
    wait_for(lambda: not w._subtitle_busy)
    assert w._subtitle_menu is None and feedback == []
    movie.gate = None
    menu = open_menu(w)
    assert not any("English" in a.text() for a in menu.actions())


def test_menu_prevents_overlay_auto_hide(player):
    w, _, _ = player
    menu = open_menu(w)
    w.overlay.show()
    w._on_hide_timeout()
    assert menu.isVisible() and w.overlay.isVisible()


def test_close_during_query_does_not_reopen_menu_or_show_late_feedback(player):
    w, movie, feedback = player
    movie.gate = threading.Event()
    w.request_subtitle()
    wait_for(lambda: bool(movie.query_threads))
    w.close()
    movie.gate.set()
    wait_for(lambda: not w._subtitle_busy)
    assert not w.isVisible()
    assert w._subtitle_menu is None and feedback == []


def test_loading_failure_is_shown_without_success_message(player, monkeypatch):
    w, movie, feedback = player
    monkeypatch.setattr(movie, "load_local_subtitle", lambda *a, **kw: (False, "字幕加载失败"))
    monkeypatch.setattr("app.ui.widgets.video_player_window.QFileDialog.getOpenFileName",
                        lambda *a, **kw: ("invalid.srt", ""))
    action(open_menu(w), "加载本地字幕").trigger()
    wait_for(lambda: len(feedback) == 1)
    assert feedback[0][0] == "字幕加载失败"
