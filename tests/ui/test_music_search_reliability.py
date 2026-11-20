"""真实 Qt 信号/事件循环下的音乐搜索回归；不启动播放器或音源。"""
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QPushButton

from app.music.online.models import OnlineTrack
from app.ui.panels.music_panel import MusicPanel
from app.ui.panels.settings_panel import SettingsPanel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    # Match main.py: initialize the application style before business widgets.
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
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


@pytest.mark.parametrize("cards", [False, True], ids=["list", "cards"])
def test_ranked_result_actions_use_clicked_provider_and_track(panel, monkeypatch, cards):
    p, music, errors = panel
    first = OnlineTrack("来源甲", "shared", "星桥", "远舟")
    live = OnlineTrack("来源甲", "live", "星桥（Live）", "纸鸢乐队")
    exact = OnlineTrack("来源乙", "shared", "星桥", "纸鸢乐队", extra={"raw": {"asset": "fixture"}})
    music.search_online = lambda keyword: {"fake": [first, live, exact]}
    music.get_playlists = lambda: [{"id": 12, "name": "我喜欢的音乐"}]
    music.is_downloaded = lambda title, artist: False
    music.play_online = Mock(return_value=True)
    music.add_next = Mock(return_value=True)
    music.add_to_playlist = Mock(return_value=True)
    music.download_song = Mock()
    p.resize(1180, 760)
    p.show()
    p.tabs.setCurrentIndex(1)
    p.edit_kw.setText("星桥 纸鸢乐队")
    p.btn_search.click()
    wait_until(lambda: p._thread is None and p.list_results.count() == 3)

    p._toggle_view_mode(not cards)
    p._toggle_view_mode(cards)
    assert p._tracks_by_row == [exact, live, first]
    item = p.list_results.item(0)
    assert "来源乙" in item.text()
    assert "来源乙" in item.data(Qt.UserRole)["_sub"]
    assert "Live" in p.list_results.item(1).text()
    p.list_results.scrollToItem(item)
    QApplication.processEvents()
    point = p.list_results.visualItemRect(item).center()
    assert p.list_results.itemAt(point) == item

    p.list_results.itemDoubleClicked.emit(item)
    wait_until(lambda: "正在播放" in p.lbl_status.text())
    music.play_online.assert_called_once_with(exact)
    assert music.play_online.call_args.args[0] is exact

    def invoke_actions(menu, _position):
        actions = {action.text(): action for action in menu.actions()}
        actions["下一首播放"].trigger()
        playlist_menu = actions["添加到歌单 ▸"].menu()
        next(action for action in playlist_menu.actions() if action.text() == "我喜欢的音乐").trigger()
        actions["下载到本地"].trigger()

    monkeypatch.setattr(QMenu, "exec_", invoke_actions)
    p._on_online_context_menu(point)

    assert music.add_next.call_count == music.add_to_playlist.call_count == music.download_song.call_count == 1
    playlist_id, saved = music.add_to_playlist.call_args.args
    assert playlist_id == 12
    for song in (music.add_next.call_args.args[0], saved, music.download_song.call_args.args[0]):
        assert (song["source_name"], song["track_id"], song["artist"]) == ("来源乙", "shared", "纸鸢乐队")
    assert music.download_song.call_args.args[0]["extra"] == exact.extra
    assert errors == []


def test_ranked_filters_use_submitted_query_and_original_provider_groups(panel):
    from app.music.online.models import SearchResults

    p, music, errors = panel
    other = OnlineTrack("来源甲", "1", "星桥（Live）", "远舟")
    exact = OnlineTrack("来源乙", "2", "星桥（Live）", "纸鸢乐队")
    cover = OnlineTrack("来源乙", "3", "星桥（翻唱）", "纸鸢乐队")
    result = SearchResults()
    result.update(fake=[other], bridge=[exact, cover], offline=[])
    result.successful_sources.update(("fake", "bridge"))
    result.errors["offline"] = "network"
    music.get_online_sources = lambda: ["fake", "bridge", "offline"]
    music.search_online = lambda keyword: result
    p.edit_kw.setText("星桥 Live 纸鸢乐队")
    p.edit_kw.returnPressed.emit()
    wait_until(lambda: p._thread is None and p.list_results.count() == 3)
    assert p._tracks_by_row == [exact, other, cover]
    assert "部分音源失败" in p.lbl_status.text()

    p.edit_kw.setText("星桥 Live 远舟")
    p.cb_search_filter.setCurrentIndex(1)
    assert p._tracks_by_row == [exact, other]
    p.cb_source.setCurrentIndex(p.cb_source.findData("bridge"))
    p._apply_online_filter()
    assert p._tracks_by_row == [exact]
    p.cb_search_filter.setCurrentIndex(2)
    assert p._tracks_by_row == [cover]
    assert errors == []


def test_identify_and_discovery_keep_source_order_after_ranked_search(panel):
    p, music, errors = panel
    first = OnlineTrack("fake", "1", "别的歌曲", "远舟")
    exact = OnlineTrack("fake", "2", "星桥", "纸鸢乐队")
    music.search_online = lambda keyword: {"fake": [first, exact]}
    p.edit_kw.setText("星桥")
    p.btn_search.click()
    wait_until(lambda: p._thread is None and p._tracks_by_row == [exact, first])

    music.play_online = Mock(side_effect=[False, True])
    playback = []
    p._play_result.connect(lambda ok, message, _label: playback.append((ok, message)))
    p._on_identify_search("星桥")
    wait_until(lambda: p._thread is None and bool(playback))
    assert playback == [(True, "正在播放：星桥")]
    assert [call.args[0] for call in music.play_online.call_args_list] == [first, exact]
    p._show_discovered_tracks([first, exact], "榜单")
    p._apply_online_filter()
    assert p._tracks_by_row == [first, exact]
    assert errors == []


def test_latest_manual_search_ranks_with_its_own_keyword(panel):
    p, music, errors = panel
    first = OnlineTrack("fake", "1", "雨后信箱")
    latest = OnlineTrack("fake", "2", "星桥")

    def search(keyword):
        if keyword == first.title:
            music.entered.set()
            assert music.release.wait(3)
        return {"fake": [first, latest]}

    music.search_online = search
    p.edit_kw.setText(first.title)
    p.btn_search.click()
    wait_until(music.entered.is_set)
    p.edit_kw.setText(latest.title)
    p.btn_search.click()
    music.release.set()
    wait_until(lambda: p._thread is None and bool(p.displays))
    assert p.displays == [[latest.title, first.title]]
    assert p._tracks_by_row == [latest, first]
    assert errors == []
