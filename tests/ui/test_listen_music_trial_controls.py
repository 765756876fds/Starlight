"""Real queued Qt switch/status; no user application startup or microphone."""

import threading
import time
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import pytest

from app.music.goal_adapter import GoalAdapter
from app.ui.theme.manager import get_theme_manager
from app.ui.widgets.listen_music_trial_controls import ListenMusicTrialControls
from app.voice.listen_music_trial import ListenMusicTrial
from tests.unit.test_goal_admission import setup as setup  # noqa: F401


@pytest.mark.parametrize("mode,scale", [("light", 1), ("dark", 1.25), ("light", 1.5)])
def test_user_toggle_and_current_queued_state_are_process_only(setup, mode, scale):
    c, player, _, _, _ = setup
    app = QApplication.instance()
    theme = get_theme_manager(app)
    theme.set_theme(mode, persist=False)
    trial = ListenMusicTrial(GoalAdapter.for_user_trial(c))
    before = c.db._conn.total_changes
    widget = ListenMusicTrialControls(trial)
    widget.resize(round(380 * scale), round(260 * scale))
    widget.show()
    try:
        assert not widget.toggle.isChecked() and "V1" in widget.status.text()
        with (patch.object(c, "goal_state", side_effect=AssertionError("GUI IPC")),
              patch.object(trial.adapter.resources, "capture", side_effect=AssertionError("GUI IO"))):
            widget.toggle.click()
            assert trial.snapshot().enabled and widget.toggle.isChecked()
            trial._legacy_unresolved = True
            widget.toggle.click()
            app.processEvents()
            assert not trial.snapshot().enabled and not widget.toggle.isChecked()
            assert "保护" in widget.status.text() and "已成功" not in widget.status.text()
            # Old queued false/true payloads cannot resurrect the switch.
            assert c.db._conn.total_changes == before and not player.loads
        assert not ListenMusicTrial(GoalAdapter.for_user_trial(c)).snapshot().enabled
    finally:
        trial.set_listener(None)
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_background_status_is_queued_and_gui_remains_responsive(setup, monkeypatch, tmp_path):
    import json

    c, _, _, _, _ = setup
    trial = ListenMusicTrial(GoalAdapter.for_user_trial(c))
    app = QApplication.instance()
    gui = threading.get_ident()
    threads = []
    original = ListenMusicTrialControls._refresh
    def refresh(self, state=None):
        threads.append(threading.get_ident())
        return original(self, state)
    monkeypatch.setattr(ListenMusicTrialControls, "_refresh", refresh)
    widget = ListenMusicTrialControls(trial)
    widget.show()
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(5)
    started, release = threading.Event(), threading.Event()
    def worker():
        trial.set_enabled(True)
        trial.permit("我想听歌了", {}, "owned-op")
        with trial._lock:
            trial._active = True
        trial._announce()
        started.set()
        assert release.wait(1.5)
        with trial._lock:
            trial._active = False
        trial._announce()
    thread = threading.Thread(target=worker, name="owned-trial-status")
    try:
        thread.start()
        deadline = time.monotonic() + 1
        while (not started.is_set() or len(ticks) < 15) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.001)
        assert len(ticks) >= 15 and started.is_set()
        assert "执行或观察" in widget.status.text()
        assert threads and set(threads) == {gui}
        release.set()
        thread.join(1)
        assert not thread.is_alive()
        app.processEvents()
        assert "B5" in widget.status.text()
        (tmp_path / "qt-status-response.json").write_text(json.dumps(dict(
            ticks=len(ticks), max_gap_ms=max((b-a)*1000 for a,b in zip(ticks,ticks[1:])),
            gui_thread_only=set(threads) == {gui})))
    finally:
        release.set()
        thread.join(2)
        timer.stop()
        trial.set_listener(None)
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_actual_settings_voice_tab_contains_process_only_trial_control(monkeypatch):
    from app.ui.panels.settings_panel import SettingsPanel

    # Isolate unrelated settings builders; invoke the real constructor/tab wiring.
    for name in ("_build_music_group", "_build_plugins_group", "_build_movie_group",
                 "_build_lyrics_group", "_build_tts_group", "_build_lol_group",
                 "_build_ui_group", "_build_reset_row"):
        monkeypatch.setattr(SettingsPanel, name, lambda *args: None)
    panel = SettingsPanel()
    try:
        assert panel._tabs.tabText(1) == "语音"
        controls = panel.listen_music_trial_controls
        assert panel._tabs.widget(1).isAncestorOf(controls)
        assert not controls.toggle.isEnabled()
    finally:
        panel.close()
        panel.deleteLater()
        QApplication.instance().processEvents()
