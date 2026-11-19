from dataclasses import replace
import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QPushButton, QWidget

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


def test_view_sources_is_existing_nonmodal_read_only_and_does_not_play(scene, monkeypatch):
    row, notice = prepare(scene, monkeypatch)
    manager = Mock()
    manager.get_active_name.return_value = "One"
    manager.get_all_sources.return_value = [
        {"name": "One", "url": "https://synthetic.invalid/api?token=secret", "enabled": True},
        {"name": "Two", "url": "https://another.invalid/api", "enabled": True},
    ]
    scene.controller._source_manager = manager
    scene.window._poll_quality_notice()
    scene.mpv.reset_mock()
    notice.view_button.click()
    dialog = scene.window._quality_sources_dialog
    assert dialog.isVisible() and not dialog.isModal()
    assert dialog.windowModality() == Qt.NonModal
    assert dialog.list_widget.count() == 2 and dialog._test_thread is None
    assert not hasattr(dialog, "btn_set_active")
    assert [b.text() for b in dialog.findChildren(QPushButton) if b.text() == "设为当前"] == []
    item = dialog.list_widget.item(1)
    assert not (item.flags() & Qt.ItemIsUserCheckable)
    assert "secret" not in item.text()
    dialog.list_widget.itemDoubleClicked.emit(item)
    scene.mpv.loadfile.assert_not_called()
    scene.mpv.stop.assert_not_called()
    assert manager.method_calls == [
        ("get_active_name", (), {}), ("get_all_sources", (), {}),
    ]


def test_old_notice_button_cannot_open_sources_after_seek_or_off(scene, monkeypatch):
    row, notice = prepare(scene, monkeypatch)
    scene.window._poll_quality_notice()
    row[0] = evidence(state="EXCLUDED", reason="seek")
    notice.view_button.click()
    assert scene.window._quality_sources_dialog is None
    row[0] = evidence()
    scene.panel.quality_trial_action.trigger()
    notice.view_button.click()
    assert scene.window._quality_sources_dialog is None


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
