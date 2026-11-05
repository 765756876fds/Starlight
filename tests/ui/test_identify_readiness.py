"""首次使用和连续识曲的真实 Qt 入口；不录音、不联网。"""
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from unittest.mock import Mock
from PySide6.QtWidgets import QApplication

from app.ui.dialogs import identify_dialog as module

pytestmark = pytest.mark.ui


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    service = SimpleNamespace(record_seconds=8, has_library=lambda: False)
    dlg = module.IdentifyDialog(service)
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(dlg, "_start_identify_worker", lambda *a: None)
    yield dlg
    if dlg._timer:
        dlg._timer.stop()
    dlg._cleanup_wav()
    dlg.hide()
    app.processEvents()


def test_empty_library_can_begin_online_identification(dialog, monkeypatch):
    build = []
    monkeypatch.setattr(dialog, "_show_build", lambda: build.append(True))
    dialog.btn_start.click()
    assert dialog.is_busy(), "在线识曲不应要求先拥有本地歌曲或指纹库"
    assert Path(dialog._current_wav).is_file()
    assert build == []


def test_retry_button_restarts_capture(dialog):
    dialog.btn_retry.click()
    assert dialog.is_busy(), "重新识别应真正开始一次新采集"


def test_recordings_have_independent_owned_files(dialog):
    dialog._begin_record()
    first = Path(dialog._current_wav)
    first.write_bytes(b"first recording")
    dialog._on_cancelled()
    dialog._begin_record()
    second = Path(dialog._current_wav)
    assert second != first
    assert not first.exists()
    assert second.exists()


def test_cleanup_never_guesses_a_previous_recording(dialog, tmp_path, monkeypatch):
    previous = tmp_path / f"humming_{module.os.getpid()}.wav"
    previous.write_bytes(b"not owned by this request")
    dialog._cleanup_wav()
    assert previous.read_bytes() == b"not owned by this request"


def test_online_success_feedback_does_not_deny_parent_playback(dialog):
    searches = []
    dialog._on_search = searches.append
    dialog._on_online_done(SimpleNamespace(
        status=module.ShazamRecognitionStatus.SUCCESS,
        title="测试歌曲", artist="测试歌手", track_id="1", url=""))
    assert searches == ["测试歌曲 测试歌手"]
    assert "不会自动播放" not in dialog.lbl_status.text()


def test_online_upload_is_explained_before_capture(dialog):
    dialog.start()
    assert "在线" in dialog.lbl_status.text()
    assert "发送" in dialog.lbl_status.text()


@pytest.mark.parametrize("outcome, expected", [
    (False, "未能播放"), (None, "尚未确认"), (True, "已确认"),
])
def test_local_playback_feedback_uses_the_actual_callback_result(dialog, outcome, expected):
    dialog._result = {"filepath": "isolated-test.wav"}
    dialog._on_play = lambda result: outcome
    dialog._on_play_clicked()
    assert expected in dialog.lbl_status.text()


def test_local_playback_exception_is_visible(dialog):
    dialog._result = {"filepath": "isolated-test.wav"}
    def failed(result):
        raise RuntimeError("test failure")
    dialog._on_play = failed
    dialog._on_play_clicked()
    assert "未能播放" in dialog.lbl_status.text()


@pytest.mark.parametrize("outcome", [True, False, None])
def test_local_playback_adapter_preserves_controller_result(outcome):
    from app.ui.panels.music_panel import MusicPanel
    song = {"path": "isolated-test.wav"}
    music = SimpleNamespace(db=SimpleNamespace(get_song_by_path=lambda _: song),
                            play_song=Mock(return_value=outcome))
    panel = SimpleNamespace(_music=music)
    assert MusicPanel._on_identify_play(panel, {"filepath": song["path"]}) is outcome
    music.play_song.assert_called_once_with(song)


def test_stale_fingerprint_path_reports_failure_without_playing():
    from app.ui.panels.music_panel import MusicPanel
    music = SimpleNamespace(db=SimpleNamespace(get_song_by_path=lambda _: None), play_song=Mock())
    assert MusicPanel._on_identify_play(SimpleNamespace(_music=music),
                                       {"filepath": "missing-test.wav"}) is False
    music.play_song.assert_not_called()


def test_empty_library_worker_captures_without_local_lookup(tmp_path):
    captures, lookups, results = [], [], []
    service = SimpleNamespace(
        capture_audio=lambda path, **kw: captures.append((path, kw)),
        record_and_identify=lambda *a, **kw: lookups.append(a))
    worker = module._IdentifyWorker(service, str(tmp_path / "clip.wav"),
                                   "system", None, skip_local=True)
    worker.done.connect(results.append)
    worker.run()
    assert len(captures) == 1
    assert captures[0][1]["source"] == "system"
    assert lookups == []
    assert results == [None], "采集成功后通过现有未命中路径进入在线识曲"


def test_real_worker_finishes_then_old_finished_cannot_delete_new_audio(dialog, monkeypatch):
    app = QApplication.instance()
    captures = []
    dialog._service.capture_audio = lambda path, **kw: captures.append(path)
    dialog._service.record_and_identify = lambda *a, **kw: pytest.fail("不应查询空指纹库")
    monkeypatch.setattr(dialog, "_start_identify_worker",
                        module.IdentifyDialog._start_identify_worker.__get__(dialog))
    monkeypatch.setattr("app.music.shazamio_provider.ShazamIOProvider",
                        lambda: SimpleNamespace(recognize=lambda path: SimpleNamespace(
                            status=module.ShazamRecognitionStatus.NO_MATCH, detail="")))
    dialog.begin()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        app.processEvents()
        threads = [t for t in (dialog._identify_thread, dialog._online_thread) if t is not None]
        if not dialog.is_busy() and all(not t.isRunning() for t in threads):
            break
        time.sleep(0.005)
    assert len(captures) == 1
    assert not dialog.is_busy()
    assert all(not t.isRunning() for t in threads)
    assert "未找到匹配" in dialog.lbl_status.text()
    assert not Path(captures[0]).exists()
    old_thread = dialog._online_thread
    monkeypatch.setattr(dialog, "_start_identify_worker", lambda *a: None)
    dialog.begin()
    new_audio = Path(dialog._current_wav)
    old_thread.finished.emit()  # 模拟晚到的上一轮收尾通知。
    app.processEvents()
    assert new_audio.exists()
    assert dialog.is_busy()
