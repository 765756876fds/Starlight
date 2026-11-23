"""Real Qt controls with synthetic tracks and isolated preference files."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from app.ui import settings_store
from app.ui.panels.music_panel import MusicPanel
from app.music.online.models import OnlineTrack
from tests.ui.test_music_search_reliability import qapp, panel, wait_until

pytestmark = pytest.mark.ui


@pytest.fixture
def prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr(settings_store, "UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    return tmp_path


@pytest.mark.parametrize("grid", [True, False])
def test_primary_download_uses_clicked_identity_and_default_best(panel, prefs, grid):
    p, music, errors = panel
    calls = []
    music.download_song = lambda *a, **kw: calls.append((a, kw)) or True
    t = OnlineTrack("chosen", "exact", "星桥", "纸鸢", "夜航", 180, extra={"raw": {"id": "exact"}})
    other = OnlineTrack("chosen", "other", "另一首", "不同歌手")
    p.resize(920, 710)
    p.show()
    p.tabs.setCurrentIndex(1)
    p._on_search_done({"chosen": [other, t]})
    p.online_stack.setCurrentWidget(p.results_page)
    p._toggle_view_mode(grid)
    QTest.qWait(15)
    p.list_results.setCurrentRow(0)
    item = p.list_results.item(1)
    rect = p.list_results.visualItemRect(item)
    point = p._dl_results.action_rect(rect).center()
    QTest.mouseClick(p.list_results.viewport(), Qt.LeftButton, pos=point)
    assert len(calls) == 1
    song = calls[0][0][0]
    assert (song["track_id"], song["album"], song["duration_seconds"]) == ("exact", "夜航", 180)
    assert song["extra"] == t.extra and calls[0][1] == {}
    assert "正在下载" in p.lbl_status.text()
    assert errors == []


def test_finish_from_worker_reports_audio_and_missing_attachment_separately(panel, prefs):
    p, music, errors = panel
    holder = {}
    def download(song, callbacks, **kw):
        holder.update(callbacks)
        return True
    music.download_song = download
    p._on_download_song(dict(title="星桥", artist="纸鸢", source_type="online"))
    assert "完成" not in p.lbl_status.text()
    worker = threading.Thread(target=lambda: (
        holder["on_details"]("星桥|纸鸢", dict(quality="无损", lyrics="unavailable", indexed=True)),
        holder["on_done"]("星桥|纸鸢", "synthetic.wav")))
    worker.start()
    worker.join(2)
    wait_until(lambda: "下载完成" in p.lbl_status.text())
    assert "无损" in p.lbl_status.text() and "歌词暂缺" in p.lbl_status.text()
    assert errors == []


def test_failure_hides_technical_detail_and_offers_next_step(panel, prefs):
    p, music, errors = panel
    def download(song, callbacks):
        callbacks["on_fail"]("星桥|纸鸢", "HTTP 403 fixture private internals")
        return False
    music.download_song = download
    p._on_download_song(dict(title="星桥", artist="纸鸢"))
    assert "重试" in p.lbl_status.text() and "其他版本" in p.lbl_status.text()
    assert "403" not in p.lbl_status.text()
    assert errors == []


def test_custom_dialog_persists_only_on_accept(prefs, qapp):
    from app.ui.dialogs.download_options_dialog import DownloadOptionsDialog
    verified = [("high", ".mp3", "320 kbps"), ("super", ".flac", "无损")]
    d = DownloadOptionsDialog(verified=verified)
    d.format.setCurrentIndex(d.format.findData(".mp3"))
    d.quality.setCurrentIndex(1)
    assert d.quality.currentText() == "320 kbps"
    assert d.quality.count() == 2
    d.choices["cover"].setChecked(False)
    d.reject()
    assert settings_store.load_download_prefs()["quality"] == "best"
    d = DownloadOptionsDialog(verified=verified)
    d.format.setCurrentIndex(d.format.findData(".mp3"))
    d.quality.setCurrentIndex(1)
    d.choices["cover"].setChecked(False)
    d._accept()
    restored = DownloadOptionsDialog(verified=verified)
    assert restored.options()["quality"] == "high"
    assert restored.options()["format"] == ".mp3"
    assert restored.options()["cover"] is False
    unknown = DownloadOptionsDialog()
    assert unknown.format.count() == unknown.quality.count() == 1
    assert unknown.options()["quality"] == "best"
    unknown._accept()
    assert settings_store.load_download_prefs()["format"] == ".mp3"
    for w in (d, restored, unknown):
        w.close()


def test_lyric_quick_controls_preserve_position_and_notify_live(panel, prefs, monkeypatch):
    from app.ui.panels import settings_panel
    p, _, errors = panel
    listener = Mock()
    monkeypatch.setattr(settings_panel, "_LYRICS_LISTENERS", [listener])
    settings_store.save_lyrics_prefs(dict(pos_x=130, pos_y=170, font_size=24, locked=True))
    p._build_lyrics_menu()
    p.lyrics_actions["字号放大"].trigger()
    updated = settings_store.load_lyrics_prefs()
    assert (updated["font_size"], updated["pos_x"], updated["pos_y"]) == (26, 130, 170)
    assert listener.called
    assert "pos_x" not in listener.call_args.args[0]
    p._build_lyrics_menu()
    p.lyrics_actions["歌词延后 0.2 秒"].trigger()
    assert settings_store.load_lyrics_prefs()["offset_ms"] == 200
    p._build_lyrics_menu()
    p.lyrics_actions["单行歌词"].trigger()
    assert settings_store.load_lyrics_prefs()["mode"] == "single"
    p._build_lyrics_menu()
    p.lyrics_actions["锁定桌面歌词"].trigger()
    assert settings_store.load_lyrics_prefs()["locked"] is False
    p._build_lyrics_menu()
    p.lyrics_actions["恢复同步"].trigger()
    assert settings_store.load_lyrics_prefs()["offset_ms"] == 0
    assert errors == []


@pytest.mark.parametrize("offset, expected", [(0, 1), (200, 0), (-200, 1)])
def test_lyric_offset_changes_display_only(qapp, prefs, offset, expected):
    from app.ui.widgets.lyrics_window import LyricsWindow
    ctrl = SimpleNamespace(get_current_song=lambda: None, current_playback_time=1.1)
    w = LyricsWindow(ctrl)
    w.set_lyrics([(0, "前一行"), (1, "当前行")], "星桥")
    w.apply_prefs(dict(offset_ms=offset))
    w._sync()
    assert w._current_idx == expected
    assert ctrl.current_playback_time == 1.1
    w.close()


@pytest.mark.parametrize("value, expected", [("invalid", 0), (None, 0), (99999, 10000), (-99999, -10000)])
def test_malformed_offset_is_safe(prefs, value, expected):
    settings_store.save_lyrics_prefs(dict(offset_ms=value))
    assert settings_store.load_lyrics_prefs()["offset_ms"] == expected


def test_settings_reopens_with_quick_changes_without_writing(prefs, qapp, monkeypatch):
    from tests.ui.test_settings_tabs import _make_panel
    from app.ui.panels import settings_panel
    p = _make_panel(qapp)
    settings_store.save_lyrics_prefs(dict(font_size=36, locked=False, mode="single", pos_x=100, pos_y=150))
    writer = Mock(side_effect=AssertionError("opening settings must not save"))
    monkeypatch.setattr(settings_panel, "save_lyrics_prefs", writer)
    p.show()
    QTest.qWait(5)
    assert p.spin_lyrics_size.value() == 36
    assert not p.cb_lyrics_locked.isChecked()
    assert p.cmb_lyrics_mode.currentData() == "single"
    writer.assert_not_called()
    p.close()

def test_tasks_retry_exact_request_and_open_published_folder(panel, prefs, monkeypatch):
    from app.ui.dialogs.download_tasks_dialog import QDesktopServices

    p, music, errors = panel
    invocations = []
    music.download_song = lambda song, callbacks, **options: invocations.append((song, callbacks, options)) or True
    song = dict(title="星桥", artist="纸鸢", album="现场场次", source_name="chosen", track_id="exact", extra={"raw": 1})
    options = dict(format=".wav", quality="best")
    p._on_download_song(song, options=options)
    p._on_download_song(song, options=options)
    assert len(invocations) == 1
    old = invocations[0][1]
    old["on_progress"]("", 47)
    tasks = p._download_pane
    assert "当前文件 47%" in tasks.items.item(0).text()
    old["on_fail"]("", "音频未能完整下载")
    assert tasks.retry.isEnabled()
    tasks.retry.click()
    assert len(invocations) == 2 and invocations[1][0] == song
    assert invocations[1][2] == {"options": options}
    old["on_done"]("", "old.wav")
    assert not tasks.open_folder.isEnabled()
    cb = invocations[1][1]
    path = prefs / "星桥.wav"
    path.write_bytes(b"published")
    cb["on_details"]("", dict(format="WAV", quality="无损", size_bytes=2048))
    cb["on_done"]("", str(path))
    assert "WAV · 无损 · 2.0 KiB" in tasks.items.item(0).text()
    opened = Mock(return_value=True)
    monkeypatch.setattr(QDesktopServices, "openUrl", opened)
    tasks.open_folder.click()
    assert opened.call_args.args[0].toLocalFile() == prefs.as_posix()
    path.unlink()
    tasks.open_folder.click()
    assert opened.call_count == 1 and "已移动或删除" in tasks.hint.text()
    assert not errors


def test_playing_lyrics_shares_clock_and_scrolls_without_seeking(panel, prefs):
    p, music, errors = panel
    p.show()
    p.btn_playing_lyrics.click()
    engine = p._lyrics_window
    view = p.playing_lyrics
    assert view.message.text() == "播放歌曲后显示歌词"
    engine.set_lyrics([], "没有歌词的歌曲")
    assert view.message.text() == "这首歌暂无歌词"
    engine.set_lyrics([(float(i), f"第{i}行") for i in range(40)], "星桥")
    music.current_playback_time = 22.2
    engine._sync()
    assert view.active_line == 22 and view.lines.item(22).font().bold()
    old_size = view.lines.item(22).font().pixelSize()
    p._build_lyrics_menu()
    p.lyrics_actions["字号放大"].trigger()
    assert view.lines.item(22).font().pixelSize() > old_size
    view._pause_follow()
    view.lines.verticalScrollBar().setValue(0)
    music.current_playback_time = 23.4
    engine._sync()
    assert view.lines.verticalScrollBar().value() == 0
    view.follow.click()
    assert view.lines.verticalScrollBar().value() > 0
    p.btn_lyrics.click()
    assert p._lyrics_window is engine and engine._started
    p.btn_playing_lyrics.click()
    assert engine._started
    p.btn_lyrics.click()
    assert not engine._started and music.current_playback_time == 23.4
    assert not errors

def test_custom_check_updates_only_open_dialog(qapp, prefs):
    from app.ui.dialogs.download_options_dialog import DownloadOptionsDialog

    calls = []
    dialog = DownloadOptionsDialog(probe=lambda ready, cancelled: calls.append((ready, cancelled)) or True)
    dialog.show()
    dialog.check_options.click()
    assert not dialog.check_options.isEnabled()
    ready, cancelled = calls[-1]
    assert not cancelled()
    ready([("high", ".mp3", "320 kbps")])
    assert dialog.info.isHidden() and dialog.format.findData(".mp3") > 0
    dialog.check_options.click()
    ready, cancelled = calls[-1]
    dialog.reject()
    assert cancelled()
    ready([("super", ".flac", "无损")])
    assert dialog.format.findData(".flac") == -1
