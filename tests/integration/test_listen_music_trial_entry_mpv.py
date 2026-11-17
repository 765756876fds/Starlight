"""User switch -> existing voice bus -> real owned MPV, local chart fixture only."""

import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from app.core.operation_result import OperationStatus as S
from app.ui.widgets.listen_music_trial_controls import ListenMusicTrialControls
from tests.integration.test_goal_adapter_mpv import charts
from tests.integration.test_goal_admission_mpv import run_worker
from tests.integration.test_music_shutdown_restart_mpv import progressing
from tests.unit.test_listen_music_trial import attach, joined, speak
from tests.unit.test_listen_music_trial_entry import assemble
from tools.goal_execute_b3.lab import Lab

pytestmark = pytest.mark.integration


def test_actual_process_switch_legacy_b5_resume_disable_and_cleanup(tmp_path, monkeypatch):
    app = QApplication.instance()
    record = {}
    # Creation/UI operations stay on GUI; business work runs through the old bus.
    with Lab(tmp_path / "中文入口隔离", os.environ["STARLIGHT_TEST_MPV"]) as lab:
        c = lab.ctrl
        c.state_store.set("movie", "playing", False)
        charts(lab)
        rt, trial, _, _, values, workers = attach(c, lab.root, monkeypatch)
        application, trial = assemble(c, rt)
        assert application.voice.listen_music_trial is trial
        controls = ListenMusicTrialControls(trial)
        controls.show()
        try:
            with (patch.object(c.mpv, "_send_command", wraps=c.mpv._send_command) as rpc,
                  patch.object(c, "admit_music_goal", wraps=c.admit_music_goal) as admit):
                speak(rt, "我想听歌了")
                joined(workers)
                assert values[-1][0].status is S.CONFIRMED
                assert trial.snapshot().route == "V1" and not admit.called
                run_worker(lambda: progressing(c, c.mpv.get_property("path")))
                controls.toggle.click()
                speak(rt, "我想听歌了")
                joined(workers)
                assert values[-1][0].outcome.decision.plan == "NO_OP"
                assert values[-1][0].outcome.receipt.proof.position > values[-1][0].outcome.receipt.proof.previous_position
                assert trial.snapshot().route == "B5"
                run_worker(c.pause)
                speak(rt, "我想听歌了")
                joined(workers)
                assert values[-1][0].outcome.decision.plan == "RESUME_MUSIC"
                assert values[-1][0].status is S.CONFIRMED
                assert len([call for call in rpc.call_args_list if call.args[0] == "loadfile"]) == 1
                controls.toggle.click()
                assert not trial.snapshot().enabled
                speak(rt, "我想听歌了")
                joined(workers)
                assert values[-1][0].status is S.CONFIRMED and trial.snapshot().route == "V1"
                assert admit.call_count == 2
                app.processEvents()
                assert "V1" in controls.status.text()
                record.update(route=trial.snapshot().route, admissions=admit.call_count,
                              loads=len([call for call in rpc.call_args_list if call.args[0] == "loadfile"]),
                              playback_confirmed=True)
        finally:
            joined(workers)
            rt.stop()
            trial.adapter.close()
            trial.set_listener(None)
            controls.close()
            controls.deleteLater()
            app.processEvents()
    assert lab.cleanup == dict(threads_remaining=[], process_exited=True)
    record["cleanup"] = lab.cleanup
    (tmp_path / "entry-real-mpv.json").write_text(json.dumps(record))


def test_enabled_cold_user_policy_uses_original_chart_before_admission(tmp_path, monkeypatch):
    def body():
        with Lab(tmp_path / "trial-cold", os.environ["STARLIGHT_TEST_MPV"]) as lab:
            c = lab.ctrl
            c.state_store.set("movie", "playing", None)
            charts(lab)
            rt, trial, _, _, values, workers = attach(c, lab.root, monkeypatch)
            application, trial = assemble(c, rt)
            # Real startup has a newly constructed, never-started movie owner.
            # Model only that nonplaying owner here; no movie player is launched.
            movie = SimpleNamespace(_media_action_lock=threading.Lock(), _playback_gen=0,
                _current=None, _external_url_mode=False, _stopping=False,
                mpv=SimpleNamespace(is_running=lambda: False))
            movie._write_state = lambda **kw: c.state_store.set("movie", "playing", kw["playing"])
            application._movie_panel = SimpleNamespace(_movie=movie)
            trial.set_enabled(True)
            with (patch.object(c, "admit_music_goal", side_effect=AssertionError("only pre-admission V1")),
                  patch.object(trial.adapter.resources, "_query", side_effect=AssertionError("no private inventory"))):
                speak(rt, "我想听歌了")
                joined(workers)
                assert values[-1][0].status is S.CONFIRMED
                assert trial.snapshot().route == "V1_PRE_ADMISSION"
                assert c.state_store.get("movie", "playing") is False
                progressing(c, c.mpv.get_property("path"))
            rt.stop()
            trial.adapter.close()
        assert lab.cleanup == dict(threads_remaining=[], process_exited=True)
    run_worker(body)
