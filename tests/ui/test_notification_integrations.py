"""Observe real UI completion entry points using isolated business doubles."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTabWidget, QWidget

from app.core import notifications as module
from app.core.notifications import NotificationCenter
from app.core.operation_result import OperationResult, OperationStatus
from app.ui.panels.music_panel import MusicPanel
from app.ui.panels.settings_panel import SettingsPanel
from app.ui.dialogs.identify_dialog import IdentifyDialog, ShazamRecognitionStatus
from app.ui.widgets.video_player_window import VideoPlayerWindow


@pytest.fixture
def center(monkeypatch):
    app = QApplication.instance() or QApplication([])
    center = NotificationCenter()
    monkeypatch.setattr(module, "_center", center)
    yield center
    app.processEvents()


@pytest.mark.parametrize("status, expected", [(OperationStatus.CONFIRMED, "success"),
                                           (OperationStatus.FAILED, "failed"),
                                           (OperationStatus.UNKNOWN, "warning")])
def test_queue_append_reports_the_controller_result(center, status, expected):
    music = SimpleNamespace(add_to_queue=Mock(return_value=OperationResult(status, "歌曲入队结果")))
    panel = SimpleNamespace(_music=music, lbl_status=QLabel(),
                            _song_dict_from_row=lambda row: row)
    song = {"source_type": "online", "title": "测试歌曲"}
    MusicPanel._on_append_queue(panel, song)
    item, = center.history()
    assert item.status == expected
    music.add_to_queue.assert_called_once()


@pytest.mark.parametrize("status, expected", [(OperationStatus.CONFIRMED, "success"),
                                           (OperationStatus.FAILED, "failed")])
def test_queue_remove_reports_result_without_deleting_files(center, status, expected):
    music = SimpleNamespace(remove_from_queue=Mock(return_value=OperationResult(status, "歌曲移出结果")))
    refresh = Mock()
    panel = SimpleNamespace(_music=music, _queue_dialog=SimpleNamespace(action_status=QLabel()),
                            _schedule_queue_refresh=refresh)
    MusicPanel._remove_queue_item(panel, 3, 9)
    assert center.history()[0].status == expected
    music.remove_from_queue.assert_called_once_with(3, 9)
    refresh.assert_called_once()


@pytest.mark.parametrize("ok, expected", [(True, "success"), (False, "failed")])
def test_subtitle_real_completion_reports_success_or_failure(center, ok, expected):
    feedback = []
    panel = SimpleNamespace(_subtitle_request=1, _subtitle_busy=True,
                            overlay=SimpleNamespace(btn_subtitle=QPushButton()),
                            _subtitle_feedback=feedback.append)
    VideoPlayerWindow._on_subtitle_finished(panel, 1, ("load", (ok, "测试字幕.srt" if ok else "字幕文件无效")))
    item, = center.history()
    assert item.status == expected
    assert feedback
    assert "字幕" in item.title
    assert "srt" not in item.advice


@pytest.mark.parametrize("ok, expected", [(True, "success"), (False, "failed")])
def test_plugin_refresh_completion_uses_real_result(center, ok, expected):
    panel = SimpleNamespace(_reloading=True, btn_refresh_plugins=QPushButton(),
                            lbl_plugin_status=QLabel(), _render_plugins=Mock(),
                            _notify_online_sources_changed=Mock())
    SettingsPanel._on_refresh_done(panel, ok, 12)
    item, = center.history()
    assert item.status == expected
    assert "12" in item.message if ok else "重试" in item.message
    assert not panel._reloading
    panel._notify_online_sources_changed.assert_called_once()


@pytest.mark.parametrize("result, expected", [(ShazamRecognitionStatus.SUCCESS, "success"),
                                            (ShazamRecognitionStatus.NETWORK_ERROR, "failed"),
                                            (ShazamRecognitionStatus.DEPENDENCY_MISSING, "failed")])
def test_identify_result_is_one_correlated_notification(center, result, expected):
    service = SimpleNamespace(record_seconds=8)
    dialog = IdentifyDialog(service)
    key = center.post("正在识曲", source="identify")
    dialog._notification_id = key
    payload = SimpleNamespace(status=result, title="测试歌曲", artist="测试歌手", track_id="1", url="", detail="")
    dialog._on_online_done(payload)
    item, = center.history()
    assert item.id == key
    assert item.status == expected
    assert "测试歌曲" in item.title if expected == "success" else "未完成" in item.title
    dialog.hide()


def test_old_subtitle_completion_does_not_create_a_notification(center):
    panel = SimpleNamespace(_subtitle_request=2, _subtitle_busy=True,
                            overlay=SimpleNamespace(btn_subtitle=QPushButton()))
    VideoPlayerWindow._on_subtitle_finished(panel, 1, ("load", (True, "old.srt")))
    assert center.history() == ()


def test_main_tab_navigation_opens_existing_center(center):
    from app.ui.widgets.main_tabs import MainTabs
    tabs = QTabWidget()
    other, notifications = QWidget(), QWidget()
    tabs.addTab(other, "音乐")
    tabs.addTab(notifications, "通知中心")
    instance = SimpleNamespace(tabs=tabs, _panels={"console": notifications})
    MainTabs.show_notifications(instance)
    assert tabs.currentWidget() is notifications


def test_search_failure_keeps_actionable_message(center):
    key = center.post("正在搜索", source="music")
    panel = SimpleNamespace(_search_generation=3, _search_notice_ids={3: key}, lbl_status=QLabel())
    MusicPanel._on_search_failed(panel, "音源请求超时", 3)
    item, = center.history()
    assert item.status == "failed"
    assert "重试" in item.advice
    assert "音源" in item.message


def test_microphone_recovery_is_a_new_fact_without_false_command_success(center):
    from app.voice.runtime import VoiceRuntime
    runtime = VoiceRuntime({})
    runtime._record_input_failure("麦克风不可用，请检查设备")
    assert center.history()[0].status == "failed"
    module.notify("麦克风已就绪", "现在可以说出语音指令", source="microphone")
    assert [item.status for item in center.history()] == ["success", "failed"]
    assert all(item.source == "microphone" for item in center.history())


def test_late_tts_failure_belongs_to_its_original_operation(center, monkeypatch, tmp_path):
    from app.speech import tts as module_tts
    from app.core.notifications import operation_scope
    from app.ui.panels.status_bar import StatusBar
    monkeypatch.setattr(module_tts, "get_event_bus", lambda: Mock())
    tts = module_tts.TTSController({"engine": "edge", "output_dir": str(tmp_path)})
    tts._running = True
    old_ready, release_old, release_new = threading.Event(), threading.Event(), threading.Event()
    threads = []
    original_thread = threading.Thread

    def thread_factory(*a, **kw):
        thread = original_thread(*a, **kw)
        threads.append(thread)
        return thread

    monkeypatch.setattr(module_tts.threading, "Thread", thread_factory)
    publish = tts._publish_event

    def delayed_publish(kind, data):
        if threading.current_thread() is threads[0] and kind == "speech.failed":
            old_ready.set()
            assert release_old.wait(2)
        publish(kind, data)

    def generate(text):
        if text == "新播报":
            assert release_new.wait(2)
        return None

    monkeypatch.setattr(tts, "_generate_audio", generate)
    monkeypatch.setattr(tts, "_publish_event", delayed_publish)
    bar = None
    try:
        old = center.post("音乐已暂停", status="success", source="voice", phase="done")
        with operation_scope(old):
            assert tts.speak("旧播报") is True
        assert old_ready.wait(1)
        new = center.post("技能计时已创建", status="success", source="voice", phase="done")
        with operation_scope(new):
            assert tts.speak("新播报") is True
        release_old.set()
        threads[0].join(timeout=1)
        assert not threads[0].is_alive()
        assert center.get(old).speech_status == "failed"
        assert center.get(new).speech_status != "failed"
        voice = SimpleNamespace(is_mic_on=lambda: False, is_listening=lambda: False, tts_provider=lambda: tts)
        bar = StatusBar({"voice": voice})
        bar._timer.stop()
        bar.refresh()
        assert center.get(new).speech_status != "failed", "状态栏补报也不能将旧失败归给新请求"
        assert center.current_voice_id == new
    finally:
        tts._generation += 1
        release_old.set()
        release_new.set()
        for thread in threads:
            thread.join(timeout=2)
        if bar is not None:
            bar._notice_stream.dispose()
            bar.close()
