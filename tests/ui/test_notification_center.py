"""Real Qt delivery, capsule animation, history and narrow layouts."""
import threading
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from app.core import notifications as module
from app.core.notifications import NotificationCenter
from app.ui.panels.console_panel import ConsolePanel
from app.ui.panels.status_bar import StatusBar
from app.ui.widgets.voice_island import VoiceIsland
from app.ui.theme.notification_style import notification_style


@pytest.fixture
def center(monkeypatch):
    app = QApplication.instance() or QApplication([])
    center = NotificationCenter(limit=5)
    monkeypatch.setattr(module, "_center", center)
    yield center
    app.processEvents()


def test_history_clear_and_late_results(center):
    panel = ConsolePanel(center=center)
    operation = center.post("音乐已暂停", "你说：暂停音乐", status="success")
    QTest.qWait(10)
    assert panel.model.rowCount() == 1
    assert "音乐已暂停" in panel.details.toPlainText()
    assert panel.btn_clear.isEnabled()
    panel.btn_clear.click()
    QTest.qWait(10)
    assert panel.model.rowCount() == 0
    assert panel.details.toPlainText() == ""
    assert not center.update(operation, status="failed")
    panel.stream.dispose()
    panel.close()


def test_raw_log_bus_does_not_leak_into_user_notifications(center):
    from app.core.log_bus import get_log_bus
    panel = ConsolePanel(center=center)
    get_log_bus().sig_log.emit("Intent(music.pause, conf=1, params={})\nTraceback: handler IPC")
    QTest.qWait(10)
    assert panel.model.rowCount() == 0
    assert panel.details.toPlainText() == ""
    center.post("音乐已暂停", "请查看音乐页面", status="success")
    QTest.qWait(10)
    assert panel.model.rowCount() == 1
    assert "Intent" not in panel.details.toPlainText()
    panel.stream.dispose()
    panel.close()


def test_worker_notifications_render_on_gui_thread(center):
    class CheckedPanel(ConsolePanel):
        def refresh(self):
            assert QThread.currentThread() == QApplication.instance().thread()
            super().refresh()
    panel = CheckedPanel(center=center)
    thread = threading.Thread(target=lambda: center.post("字幕已加载", status="success"))
    thread.start()
    thread.join(timeout=1)
    QTest.qWait(20)
    assert not thread.is_alive()
    assert panel.model.rowCount() == 1
    assert panel.model.index(0).data(Qt.UserRole).title == "字幕已加载"
    panel.stream.dispose()
    panel.close()


def test_capsule_success_and_tts_warning_remain_independent(center):
    bar = StatusBar()
    bar._timer.stop()
    bar.show()
    operation = center.post("音乐已暂停", status="success", source="voice", phase="done")
    center.update(operation, speech_status="failed")
    QTest.qWait(220)
    assert bar.island.text() == "音乐已暂停"
    assert bar.island._status == "success"
    assert "播报未成功" in bar.island._note
    assert "语音反馈" not in bar.island.text()
    bar._notice_stream.dispose()
    bar.close()


def test_capsule_collapses_and_click_opens_history(center):
    capsule = VoiceIsland()
    capsule.COLLAPSE_MS = 25
    opened = []
    capsule.open_notifications.connect(lambda: opened.append(True))
    capsule.show()
    op = center.post("音乐已暂停", status="success", source="voice", phase="done")
    capsule.set_current(center.get(op))
    QTest.qWait(250)
    assert capsule._collapsed
    assert capsule.maximumWidth() == 44
    capsule.click()
    assert opened == [True]
    capsule.close()


def test_late_old_result_does_not_replace_new_capsule(center):
    bar = StatusBar()
    bar._timer.stop()
    old = center.post("正在搜索旧歌曲", source="voice")
    new = center.post("音乐已暂停", status="success", source="voice", phase="done")
    center.update(old, title="旧搜索完成", status="success")
    QTest.qWait(20)
    assert bar.island.text() == "音乐已暂停"
    assert center.current_voice_id == new
    bar._notice_stream.dispose()
    bar.close()


@pytest.mark.parametrize("width", [280, 480, 960, 1440])
def test_notification_panel_and_capsule_fit_small_windows(center, width):
    panel = ConsolePanel(center=center)
    panel.resize(width, 600)
    panel.show()
    center.post("电影字幕已加载", "这是较长的中文说明，用来验证窄窗口不会要求水平滚动。" * 4,
                status="success", source="voice", phase="done")
    bar = StatusBar()
    bar.resize(width, 60)
    bar.show()
    QTest.qWait(240)
    assert panel.view.horizontalScrollBar().maximum() == 0
    assert panel.view.width() <= panel.width()
    assert bar.island.width() <= bar.width()
    assert bar.island.geometry().right() <= bar.island.parentWidget().width()
    panel.stream.dispose()
    bar._notice_stream.dispose()
    bar._timer.stop()
    panel.close()
    bar.close()


def test_light_and_dark_palettes_share_semantic_roles():
    dark, light = notification_style(), notification_style("light")
    assert dark.background != light.background
    assert dark.text != light.text
    assert dark.color("success") == dark.success
    assert light.color("failed") == light.failed


def test_lcu_only_notifies_important_connection_transitions(center):
    lcu = Mock(connected=False)
    bar = StatusBar({"lcu": lcu})
    bar._timer.stop()
    assert center.history() == ()
    lcu.connected = True
    bar.refresh()
    bar.refresh()
    assert len(center.history()) == 1
    assert center.history()[0].title == "游戏客户端已连接"
    lcu.connected = False
    bar.refresh()
    assert len(center.history()) == 2
    bar._notice_stream.dispose()
    bar.close()


def test_capsule_returns_to_real_listening_state_after_result(center):
    capsule = VoiceIsland()
    capsule.set_current(listening=True)
    assert capsule.text() == "正在聆听"
    key = center.post("音乐已暂停", status="success", source="voice", phase="done")
    capsule.set_current(center.get(key), listening=True)
    assert capsule.text() == "音乐已暂停"
    capsule.collapse()
    assert capsule.text() == "正在聆听"
    capsule.set_current(center.get(key), listening=False)
    assert capsule.text() == "语音未开启"
    assert center.get(key).status == "success", "收起胶囊不能改变业务历史"
    capsule.timer.stop()
    capsule.animation.stop()
    capsule.close()


def test_fast_command_keeps_actual_recognized_words_visible(center):
    capsule = VoiceIsland()
    key = center.post("音乐已暂停", status="success", heard="暂停音乐", source="voice", phase="done")
    capsule.set_current(center.get(key))
    assert capsule.text() == "音乐已暂停"
    assert capsule._note == "你说：暂停音乐"
    assert not capsule._speech_warning
    center.update(key, speech_status="failed")
    capsule.set_current(center.get(key))
    assert capsule._speech_warning
    assert "暂停音乐" in capsule.toolTip()
    assert capsule.text() == "音乐已暂停"
    capsule.timer.stop()
    capsule.animation.stop()
    capsule.close()


def test_main_window_status_bar_does_not_clip_capsule_text(center):
    from app.ui.theme.applier import apply_glass_theme
    app = QApplication.instance()
    previous = app.styleSheet()
    bar = None
    window = QMainWindow()
    try:
        apply_glass_theme(app)
        bar = StatusBar()
        bar._timer.stop()
        window.setCentralWidget(QWidget())
        window.setStatusBar(bar)
        window.resize(800, 600)
        window.show()
        key = center.post("音乐已暂停", status="success", heard="暂停音乐", source="voice", phase="done")
        center.update(key, speech_status="failed")
        QTest.qWait(220)
        assert bar.island.height() >= 44, "主窗口状态栏不能压缩两行胶囊文字"
    finally:
        if bar is not None:
            bar._notice_stream.dispose()
        window.close()
        app.setStyleSheet(previous)
