from types import SimpleNamespace
from unittest.mock import Mock
import time
import pytest

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from app.speech.tts import TTSState
from app.ui.panels.status_bar import StatusBar
from app.core import notifications


@pytest.fixture
def notices(monkeypatch):
    center = notifications.NotificationCenter()
    monkeypatch.setattr(notifications, "_center", center)
    return center


def test_tts_failure_is_visible_and_coalesced_without_hiding_capsule(monkeypatch, notices):
    app = QApplication.instance() or QApplication([])
    tts = SimpleNamespace(get_state=lambda: TTSState.IDLE, _generation=1)
    voice = SimpleNamespace(is_mic_on=lambda: False, tts_provider=lambda: tts)
    bar = StatusBar({"voice": voice})
    bar._timer.stop()
    def assert_gui(*args):
        assert QThread.currentThread() == QApplication.instance().thread()
    show = Mock(side_effect=assert_gui)
    notices.subscribe(assert_gui)
    monkeypatch.setattr(bar, "showMessage", show)
    tts.get_state = lambda: TTSState.FAILED
    try:
        bar._timer.start(5)
        deadline = time.monotonic() + 1
        while not notices.history() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert len(notices.history()) == 1
        assert notices.history()[0].title == "语音播报未成功"
        show.assert_not_called()
        bar.refresh()
        assert len(notices.history()) == 1
        tts._generation += 1
        bar.refresh()
        assert len(notices.history()) == 1  # 连续无关联播报故障合并，避免刷屏。
    finally:
        bar._timer.stop()
        bar.close()


def test_recovered_tts_still_shows_the_previous_failure(monkeypatch, notices):
    app = QApplication.instance() or QApplication([])
    tts = SimpleNamespace(get_state=lambda: TTSState.IDLE, _generation=2,
                          get_last_failure=lambda: None)
    voice = SimpleNamespace(is_mic_on=lambda: False, tts_provider=lambda: tts)
    bar = StatusBar({"voice": voice})
    bar._timer.stop()
    show = Mock()
    monkeypatch.setattr(bar, "showMessage", show)
    tts.get_last_failure = lambda: (1, "语音播报失败：合成未完成")
    try:
        bar.refresh()
        assert len(notices.history()) == 1
        assert notices.history()[0].title == "语音播报未成功"
        show.assert_not_called()
        bar.refresh()
        assert len(notices.history()) == 1
    finally:
        bar.close()


def make_tts(tmp_path, monkeypatch, engine):
    from app.speech.tts import TTSController
    monkeypatch.setattr("app.speech.tts.get_event_bus", lambda: Mock())
    return TTSController({"engine": engine, "output_dir": str(tmp_path)})


@pytest.mark.parametrize("initial,target,voice_id", [
    ("qwen", "edge", "zh-CN-XiaoxiaoNeural"),
    ("edge", "qwen", "longanlingxin_v3.1"),
])
def test_engine_selection_updates_live_backend_before_voice_setters(tmp_path, monkeypatch, initial, target, voice_id):
    from PySide6.QtWidgets import QComboBox
    from app.ui.panels import settings_panel as module
    app = QApplication.instance() or QApplication([])
    tts = make_tts(tmp_path, monkeypatch, initial)
    previous_voice = tts.qwen_voice if target == "edge" else tts.voice
    combo = QComboBox()
    combo.addItem(initial, initial)
    combo.addItem(target, target)
    save = Mock()
    monkeypatch.setattr(module, "save_settings_yaml", save)
    panel = SimpleNamespace(cmb_engine=combo, _tts=tts,
                            _current_voice_id=lambda: voice_id,
                            _rate_slider=SimpleNamespace(value=lambda: 0),
                            _vol_slider=SimpleNamespace(value=lambda: 50),
                            lbl_rate=Mock(), lbl_vol=Mock())
    combo.currentIndexChanged.connect(lambda *_: module.SettingsPanel._on_tts_change(panel))
    combo.setCurrentIndex(1)
    assert save.call_args.args[0]["tts"]["engine"] == target
    assert tts.engine == target
    assert (tts.voice if target == "edge" else tts.qwen_voice) == voice_id
    assert (tts.qwen_voice if target == "edge" else tts.voice) == previous_voice


def test_reset_defaults_applies_the_saved_tts_engine(tmp_path, monkeypatch):
    from app.ui.panels import settings_panel as module
    tts = make_tts(tmp_path, monkeypatch, "edge")
    for name in ("reset_ui_prefs", "reset_music_dirs", "save_settings_yaml"):
        monkeypatch.setattr(module, name, Mock())
    monkeypatch.setattr(module, "load_music_dirs", lambda: [])
    monkeypatch.setattr(module, "load_ui_prefs", lambda: {})
    monkeypatch.setattr(module.QMessageBox, "question", lambda *a, **k: module.QMessageBox.Yes)
    panel = SimpleNamespace(_tts=tts, _render_dirs=Mock(), _load_tts_values=Mock(),
                            btn_gate=Mock(), btn_equip=Mock(), btn_overlay=Mock(),
                            spin_refresh=Mock(), _overlay_toggle=None, _on_refresh_changed=None)
    module.SettingsPanel._on_reset_defaults(panel)
    assert tts.engine == "qwen"
    assert tts.qwen_voice == module._QWEN_DEFAULT_VOICE
    assert tts.voice == "zh-CN-XiaoxiaoNeural"
