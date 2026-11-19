from dataclasses import replace
import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QPushButton, QWidget

from app.movie.source_quality import QualityEvidence
from tests.ui.test_movie_voice_window import scene as scene
from tests.ui.test_movie_voice_window import wait_for

pytestmark = pytest.mark.ui


def evidence(**values):
    return replace(QualityEvidence("synthetic", "synthetic", 1, time.monotonic(),
                                  state="BUFFERING", reason="continuous_cache_wait",
                                  progress_s=10, waits=1, wait_lower_s=4,
                                  advisory_eligible=True), **values)


def prepare(scene, monkeypatch):
    scene.panel.quality_trial_action.trigger()
    assert scene.controller.source_quality_trial_enabled()
    scene.window._playing = True
    scene.window.show()
    scene.app.processEvents()
    row = [evidence()]
    monkeypatch.setattr(scene.controller, "source_quality_snapshot", lambda: {"current": row[0]})
    return row, scene.window.quality_notice


def test_normal_panel_assembly_has_working_process_only_switch(scene):
    s = scene
    assert "实验" in s.panel.quality_trial_action.text()
    assert not s.panel.quality_trial_action.isChecked()
    assert not s.controller.source_quality_trial_enabled()
    original = dict(s.controller._config)
    s.mpv.reset_mock()
    s.panel.quality_trial_action.trigger()
    assert s.controller.source_quality_trial_enabled()
    s.panel.quality_trial_action.trigger()
    assert not s.controller.source_quality_trial_enabled()
    assert s.controller._config == original
    assert s.mpv.mock_calls == []


@pytest.mark.parametrize("state,reason", [
    ("EXCLUDED", "seek"), ("EXCLUDED", "user_pause"), ("EXCLUDED", "eof"),
    ("EXCLUDED", "transition_grace"), ("UNKNOWN", "evidence_gap"),
    ("OBSERVING", "startup_or_recovery_wait"), ("STABLE", "verified_progress"),
])
def test_only_m21_confirmed_buffering_can_show(scene, monkeypatch, state, reason):
    row, notice = prepare(scene, monkeypatch)
    row[0] = evidence(state=state, reason=reason)
    scene.window._poll_quality_notice()
    assert not notice.isVisible()


def test_notice_does_not_steal_focus_or_repeat_and_can_be_closed(scene, monkeypatch):
    row, notice = prepare(scene, monkeypatch)
    host = QWidget()
    focus = QLineEdit(host)
    host.show()
    focus.show()
    host.activateWindow()
    focus.setFocus()
    wait_for(lambda: scene.app.focusWidget() is focus and scene.app.activeWindow() is host)
    before = scene.app.focusWidget()
    video_geometry = scene.window.container.geometry()
    scene.app.processEvents()
    assert scene.app.focusWidget() is before, "focus changed in no-notice control"
    scene.window._poll_quality_notice()
    assert notice.isVisible()
    scene.app.processEvents()
    assert scene.app.focusWidget() is before
    assert scene.window.container.geometry() == video_geometry
    assert notice.focusPolicy() == Qt.NoFocus
    assert "尚未验证" in notice.message.text()
    notice.close_button.click()
    scene.window._poll_quality_notice()
    assert not notice.isVisible()
    host.close()
    row[0] = evidence()
    assert 0 <= notice._clock() - row[0].observed_at <= 5
    scene.window._poll_quality_notice()
    assert not notice.isVisible()


def test_notice_expires_without_repeating_until_cooldown(scene, monkeypatch):
    row, notice = prepare(scene, monkeypatch)
    now = [time.monotonic()]
    notice._clock = lambda: now[0]
    row[0] = evidence(observed_at=now[0])
    scene.window._poll_quality_notice()
    assert notice.isVisible()
    now[0] += 16
    row[0] = evidence(observed_at=now[0])
    scene.window._poll_quality_notice()
    assert not notice.isVisible()
    now[0] += 105
    row[0] = evidence(observed_at=now[0])
    scene.window._poll_quality_notice()
    assert notice.isVisible()


def test_stale_new_session_off_and_recovery_hide_notice(scene, monkeypatch):
    row, notice = prepare(scene, monkeypatch)
    scene.window._poll_quality_notice()
    assert notice.isVisible()
    row[0] = evidence(session=2, advisory_eligible=False)
    scene.window._poll_quality_notice()
    assert not notice.isVisible()
    row[0] = evidence(observed_at=time.monotonic() - 10)
    scene.window._poll_quality_notice()
    assert not notice.isVisible()
    scene.panel.quality_trial_action.trigger()
    row[0] = evidence()
    scene.window._poll_quality_notice()
    assert not notice.isVisible()


def play_search_result(scene):
    s = scene
    original = dict(vod_id="1", vod_name="测试电影", vod_year="2024",
                    vod_play_url="正片$https://first.invalid/a.m3u8",
                    _source="甲来源", _source_id="first")
    alternate = dict(original, _source="乙来源", _source_id="second",
                     vod_play_url="正片$https://second.invalid/b.m3u8", vod_remarks="高清版")
    unrelated = dict(alternate, vod_id="2", vod_name="其他电影")
    old_version = dict(alternate, vod_id="3", vod_year="1999")
    s.panel._on_search_done([original, alternate, unrelated, old_version])
    s.panel.stack_results.setCurrentWidget(s.panel.list_results)
    s.panel._on_item_activated(s.panel.list_results.item(0))
    wait_for(lambda: s.controller.current_item() is not None)
    s.window._timer.stop()
    return original, alternate


def test_candidate_list_only_shows_matching_search_rows_and_waits_for_choice(scene, monkeypatch):
    s = scene
    _, alternate = play_search_result(s)
    _, notice = prepare(s, monkeypatch)
    s.window._poll_quality_notice()
    s.mpv.reset_mock()
    before = s.controller.current_item()
    selection = s.panel.list_results.currentItem()
    notice.view_button.click()
    dialog = s.panel._candidate_view[0]
    assert "影片候选" in notice.view_button.text()
    assert dialog.isVisible() and dialog.windowModality() == Qt.NonModal
    rows = dialog.findChild(QListWidget)
    assert rows.count() == 1 and "乙来源" in rows.item(0).text()
    assert "2024" in rows.item(0).text() and "高清版" in rows.item(0).text()
    assert s.panel.list_results.count() == 4
    assert s.panel.list_results.currentItem() is selection
    assert s.controller.current_item() is before
    choose = next(b for b in dialog.findChildren(QPushButton) if b.text() == "打开所选候选")
    rows.setCurrentRow(0)
    s.mpv.loadfile.assert_not_called()
    s.mpv.stop.assert_not_called()
    choose.click()
    wait_for(lambda: s.mpv.loadfile.called)
    s.mpv.loadfile.assert_called_once_with(alternate["vod_play_url"].split("$")[1])
    assert s.controller.current_item()["_source"] == "乙来源"


def test_candidate_is_rechecked_at_the_player_after_a_late_media_change(scene, monkeypatch):
    s = scene
    play_search_result(s)
    _, notice = prepare(s, monkeypatch)
    s.window._poll_quality_notice()
    notice.view_button.click()
    dialog = s.panel._candidate_view[0]
    dialog.findChild(QListWidget).setCurrentRow(0)
    choose = next(b for b in dialog.findChildren(QPushButton) if b.text() == "打开所选候选")
    previous_generation = s.controller._playback_gen
    before_play = s.window.play

    def play_after_new_episode(item, index=None):
        # This happens after the queued UI check, before MovieController.play.
        s.controller._accept_episode({"data": {"playlist_entry_id": 99}}, "https://first.invalid/new.m3u8")
        return before_play(item, index=index)

    monkeypatch.setattr(s.window, "play", play_after_new_episode)
    s.mpv.reset_mock()
    choose.click()
    wait_for(lambda: s.controller._playback_gen > previous_generation)
    s.mpv.loadfile.assert_not_called()
    s.mpv.stop.assert_not_called()


@pytest.mark.parametrize("change", ["new_search", "voice", "close", "episode", "reject"])
def test_old_candidate_view_cannot_play_after_context_changes(scene, monkeypatch, change):
    s = scene
    play_search_result(s)
    _, notice = prepare(s, monkeypatch)
    s.window._poll_quality_notice()
    notice.view_button.click()
    dialog = s.panel._candidate_view[0]
    rows = dialog.findChild(QListWidget)
    rows.setCurrentRow(0)
    choose = next(b for b in dialog.findChildren(QPushButton) if b.text() == "打开所选候选")
    if change == "new_search":
        s.panel.edit_kw.setText("新关键词")
        s.panel._do_search()
    elif change == "voice":
        from tests.ui.test_movie_voice_window import voice_command
        assert s.bus.execute(voice_command()) is True
    elif change == "episode":
        # Same series object, but a different accepted native playback generation.
        s.controller._accept_episode({"data": {"playlist_entry_id": 55}}, "https://first.invalid/ep2.m3u8")
        s.panel._on_playback_status(s.controller._playback_gen, "PENDING", "正在加载")
    elif change == "close":
        s.window.close()
    else:
        dialog.reject()
    s.mpv.reset_mock()
    if change == "voice":
        # Before queued Qt attach, the controller has already accepted new media.
        choose.click()
    wait_for(lambda: s.panel._candidate_view is None)
    s.mpv.loadfile.assert_not_called()
    s.mpv.stop.assert_not_called()


def test_no_bound_candidates_gives_precise_search_hint(scene, monkeypatch):
    s = scene
    play_search_result(s)
    s.panel._do_clear()
    _, notice = prepare(s, monkeypatch)
    s.window._poll_quality_notice()
    s.mpv.reset_mock()
    notice.view_button.click()
    dialog = s.panel._candidate_view[0]
    assert dialog.isVisible() and dialog.findChild(QListWidget) is None
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert any("重新搜索《测试电影》" in label for label in labels)
    s.mpv.loadfile.assert_not_called()
    s.mpv.stop.assert_not_called()


def test_switch_trial_can_show_plain_reason_and_manual_candidates(scene, monkeypatch):
    s = scene
    play_search_result(s)
    row, notice = prepare(s, monkeypatch)
    assert not s.window.switch_trial.enabled
    s.window.switch_trial.enabled = True
    row[0] = evidence(state="STABLE")
    s.window._poll_quality_notice()
    assert not notice.isVisible()
    row[0] = evidence(sustained_transport_wait=True, advisory_eligible=False)
    s.window._poll_quality_notice()
    assert notice.isVisible() and "没有通过验证" in notice.message.text()
    s.mpv.reset_mock()
    notice.view_button.click()
    assert s.panel._candidate_view[0].findChild(QListWidget).count() == 1
    assert s.window.switch_trial.proof is None
    s.mpv.loadfile.assert_not_called()
    s.mpv.stop.assert_not_called()


def test_old_notice_button_cannot_open_sources_after_seek_or_off(scene, monkeypatch):
    row, notice = prepare(scene, monkeypatch)
    scene.window._poll_quality_notice()
    row[0] = evidence(state="EXCLUDED", reason="seek")
    notice.view_button.click()
    assert scene.panel._candidate_view is None
    row[0] = evidence()
    scene.panel.quality_trial_action.trigger()
    notice.view_button.click()
    assert scene.panel._candidate_view is None


def test_stop_and_recovery_do_not_leave_new_prompts(scene, monkeypatch):
    row, notice = prepare(scene, monkeypatch)
    scene.window._poll_quality_notice()
    assert notice.isVisible()
    scene.window._recovering = True
    scene.window._poll_quality_notice()
    assert not notice.isVisible()
    scene.window._on_stopped()
    assert not notice.isVisible()


@pytest.mark.parametrize("crashed", [False, True])
def test_tick_checks_eof_or_dead_player_before_presenting_old_alert(scene, monkeypatch, crashed):
    row, notice = prepare(scene, monkeypatch)
    scene.window._poll_quality_notice()
    assert notice.isVisible()
    monkeypatch.setattr(scene.controller, "tick", lambda: None)
    monkeypatch.setattr(scene.controller, "is_playing", lambda: False)
    monkeypatch.setattr(scene.controller, "is_crashed", lambda: crashed)
    monkeypatch.setattr(scene.controller, "restart_current", lambda **kw: True)
    scene.window._tick()
    assert not notice.isVisible()
    assert scene.window._recovering is crashed
