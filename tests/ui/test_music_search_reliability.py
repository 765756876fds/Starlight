"""真实 Qt 信号/事件循环下的音乐搜索回归；不启动播放器或音源。"""
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from app.music.online.models import OnlineTrack
from app.ui.panels.music_panel import MusicPanel
from app.ui.panels.settings_panel import SettingsPanel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def wait_until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    assert predicate(), "搜索未在有界时间内完成"


class FakeMusic:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.fail_first = False

    def search_online(self, keyword):
        self.calls.append(keyword)
        self.entered.set()
        if self.block and len(self.calls) == 1:
            assert self.release.wait(3)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("fixture failure")
        return {"fake": [OnlineTrack("fake", keyword, keyword)]}

    def get_online_sources(self): return ["fake"]
    def get_play_mode(self): return "sequential"
    def is_highlight_mode(self): return False
    def get_volume(self): return 70
    def set_volume(self, value): pass
    def search_local(self, keyword=""): return []
    def get_current_song(self): return None
    def get_history(self, *args, **kwargs): return []


@pytest.fixture
def panel(qapp, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, exc, tb: errors.append(exc))

    class ObservedPanel(MusicPanel):
        def _on_search_done(self, *args, **kwargs):
            self.completion_threads.append(QThread.currentThread())
            super()._on_search_done(*args, **kwargs)
            self.displays.append([track.title for track in self._tracks_by_row])

        def _on_search_failed(self, *args, **kwargs):
            self.completion_threads.append(QThread.currentThread())
            super()._on_search_failed(*args, **kwargs)

    music = FakeMusic()
    p = ObservedPanel(music)
    p._timer.stop()
    p.completion_threads = []
    p.displays = []
    yield p, music, errors
    music.release.set()
    wait_until(lambda: p._thread is None or not p._thread.isRunning())
    QApplication.processEvents()
    p.close()


@pytest.mark.parametrize("entry", ["button", "return"])
def test_search_entry_completes_without_callback_typeerror(panel, entry):
    p, music, errors = panel
    p.edit_kw.setText("A")
    if entry == "button":
        p.btn_search.click()
    else:
        p.edit_kw.returnPressed.emit()
    wait_until(lambda: bool(p._tracks_by_row))
    assert music.calls == ["A"]
    assert [track.title for track in p._tracks_by_row] == ["A"]
    assert errors == []


@pytest.mark.parametrize("failure", [False, True])
def test_search_completion_updates_on_gui_thread(panel, qapp, failure):
    p, music, errors = panel
    music.fail_first = failure
    p.edit_kw.setText("A")
    p.edit_kw.returnPressed.emit()
    wait_until(lambda: bool(p.completion_threads))
    assert p.completion_threads == [qapp.thread()]
    assert errors == []


@pytest.mark.parametrize("new_keywords", [["B"], ["B", "C"]])
@pytest.mark.parametrize("first_fails", [False, True])
def test_busy_search_runs_only_latest_request_and_rejects_old_display(
        panel, new_keywords, first_fails):
    p, music, errors = panel
    music.block = True
    music.fail_first = first_fails
    p.edit_kw.setText("A")
    callbacks = []
    p._do_search(on_done=lambda tracks: callbacks.append("A"))
    wait_until(music.entered.is_set)
    for keyword in new_keywords:
        p.edit_kw.setText(keyword)
        p._do_search(on_done=lambda tracks, k=keyword: callbacks.append(k))
    music.release.set()
    latest = new_keywords[-1]
    wait_until(lambda: [track.title for track in p._tracks_by_row] == [latest])
    assert music.calls == ["A", latest]
    assert callbacks == [latest]
    assert all("A" not in batch for batch in p.displays)
    assert errors == []


def test_current_search_failure_is_visible_and_retryable(panel):
    p, music, errors = panel
    music.fail_first = True
    p.edit_kw.setText("A")
    p.edit_kw.returnPressed.emit()
    wait_until(lambda: "失败" in p.lbl_status.text())
    wait_until(lambda: p._thread is None or not p._thread.isRunning())
    p.edit_kw.returnPressed.emit()
    wait_until(lambda: bool(p._tracks_by_row))
    assert music.calls == ["A", "A"]
    assert errors == []


def test_refresh_failure_does_not_invent_timeout(qapp):
    p = SimpleNamespace(
        _reloading=True, btn_refresh_plugins=QPushButton(),
        lbl_plugin_status=QLabel(), _render_plugins=lambda: None,
        _notify_online_sources_changed=lambda: None,
    )
    SettingsPanel._on_refresh_done(p, False, 0)
    assert "失败" in p.lbl_plugin_status.text()
    assert "超时" not in p.lbl_plugin_status.text()
    assert "重试" in p.lbl_plugin_status.text()
    assert p.btn_refresh_plugins.isEnabled()


@pytest.mark.parametrize("state", ["empty", "partial", "failed", "timeout", "network", "interrupted"])
def test_request_status_survives_qt_signal_and_uses_correct_callback(panel, state):
    from app.music.online.models import SearchResults

    p, music, errors = panel
    result = SearchResults()
    result["fake"] = []
    if state in {"empty", "partial"}:
        result.successful_sources.add("fake")
    if state != "empty":
        result.errors["fake"] = {
            "timeout": "timeout", "network": "network", "interrupted": "interrupted",
        }.get(state, "plugin")
    if state == "partial":
        result["fake"] = [OnlineTrack("fake", "1", "A")]
    music.search_online = lambda keyword: result
    callbacks = []
    p.edit_kw.setText("A")
    p._do_search(on_done=lambda tracks: callbacks.append(("done", len(tracks))),
                 on_failed=lambda error: callbacks.append(("failed", error)))
    wait_until(lambda: bool(callbacks))
    text = p.lbl_status.text()
    if state == "empty":
        assert callbacks == [("done", 0)]
        assert "未找到" in text and "失败" not in text
    elif state == "partial":
        assert callbacks == [("done", 1)]
        assert "已找到 1 首" in text and "部分音源失败" in text
    else:
        assert len(callbacks) == 1 and callbacks[0][0] == "failed"
        assert "搜索失败" in text and "已找到" not in text
        assert "重试" in text
        assert ("超时" in text) == (state == "timeout")
        if state == "network":
            assert "检查网络" in text
        if state == "interrupted":
            assert "刷新或变更" in text
    assert errors == []


def test_source_filter_does_not_hide_selected_source_failure(panel):
    from app.music.online.models import SearchResults

    p, music, _ = panel
    result = SearchResults()
    result.update(fake=[], other=[OnlineTrack("other", "1", "A")])
    result.errors["fake"] = "plugin"
    result.successful_sources.add("other")
    p.cb_source.addItem("other", "other")
    p.cb_source.setCurrentIndex(p.cb_source.findData("fake"))
    p._on_search_done(result)
    assert p.list_results.count() == 0
    assert "搜索失败" in p.lbl_status.text()


def test_edit_without_submission_does_not_show_old_results_or_stay_loading(panel):
    p, music, errors = panel
    music.block = True
    p.edit_kw.setText("A")
    p.edit_kw.returnPressed.emit()
    wait_until(music.entered.is_set)
    p.edit_kw.setText("B")
    music.release.set()
    wait_until(lambda: p._thread is None)
    assert music.calls == ["A"]
    assert p.list_results.count() == 0
    assert "按回车" in p.lbl_status.text()
    assert errors == []


def test_edit_back_to_active_keyword_drops_pending_search_and_finishes_loading(panel):
    p, music, errors = panel
    music.block = True
    p.edit_kw.setText("A")
    p.edit_kw.returnPressed.emit()
    wait_until(music.entered.is_set)
    p.edit_kw.setText("B")
    p.edit_kw.returnPressed.emit()
    p.edit_kw.setText("A")  # 没有再次提交，B 不应后台执行，也不能一直显示加载。
    music.release.set()
    wait_until(lambda: p._thread is None)
    assert music.calls == ["A"]
    assert p.list_results.count() == 0
    assert "按回车" in p.lbl_status.text()
    assert errors == []


@pytest.mark.parametrize("invalid", [None, []])
def test_legacy_invalid_result_is_visible_failure_not_gui_exception(panel, invalid):
    p, music, errors = panel
    music.search_online = lambda keyword: invalid
    p.edit_kw.setText("A")
    p.edit_kw.returnPressed.emit()
    wait_until(lambda: "失败" in p.lbl_status.text())
    assert "有效搜索结果" in p.lbl_status.text()
    assert "超时" not in p.lbl_status.text()
    assert errors == []
