"""Manual play receipts in a real Qt loop, with local-only playback doubles."""
from dataclasses import replace
from threading import Event
from unittest.mock import Mock

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLabel

from app.music.next_feedback import NextFeedback, observe_manual_play, report
from tests.ui.test_music_search_reliability import panel, qapp, wait_until  # noqa: F401
from tests.ui.test_movie_voice_window import scene  # noqa: F401


@pytest.mark.parametrize("outcome,expected", [
    ("no_url", "暂时无法播放"), ("unknown", "暂未确认播放"), ("failed", "无法播放"),
])
def test_resolver_and_load_receipts(panel, outcome, expected):
    p, music, _ = panel
    entered, release = Event(), Event()
    music._play_request_id = 1

    def play():
        entered.set()
        assert release.wait(2)
        if outcome != "no_url":
            report(music, "resolve", "ready", 1)
        report(music, "resolve" if outcome == "no_url" else "loadfile", outcome, 1)
        return False

    target = Mock(side_effect=play)
    try:
        p._play_async(target, title="生成歌曲", resolving=True)
        assert entered.wait(1)
        assert "正在加载" in p.lbl_status.text()
    finally:
        release.set()
    wait_until(lambda: expected in p.lbl_status.text())
    assert p.lbl_time.text() == "0:00 / 0:00"
    target.assert_called_once_with()


@pytest.mark.parametrize("action", ["new", "hide", "close", "movie"])
def test_late_resolution_cannot_replace_current_feedback(panel, action):
    p, music, _ = panel
    entered, release, returned = Event(), Event(), Event()
    permits = [True]
    music._movie_permits_request = lambda *_: permits[0]
    music._play_request_id = 3

    def slow():
        entered.set()
        assert release.wait(2)
        report(music, "loadfile", "failed", 3)
        returned.set()
        return False

    try:
        p._play_async(slow, title="旧歌")
        assert entered.wait(1)
        if action == "new":
            p._play_async(lambda: False, title="新歌", fail_msg="新歌没有地址")
            wait_until(lambda: "新歌没有地址" in p.lbl_status.text())
        elif action == "close":
            p.close()
        elif action == "hide":
            p.show()
            p.hide()
        else:
            permits[0] = False
        text = p.lbl_status.text()
    finally:
        release.set()
    assert returned.wait(2)
    QTest.qWait(50)
    assert p.lbl_status.text() == text


def test_accepted_needs_same_new_entry_with_advancing_time(panel, monkeypatch):
    p, music, _ = panel
    music._running = True
    music._play_request_id = music._last_loadfile_epoch = 4
    music._movie_permits_request = lambda *_: True
    observer = music._next_feedback = NextFeedback(music)
    monkeypatch.setattr(observer, "_property", lambda _: [{"id": 7}])
    sample = [7, 22.5]
    monkeypatch.setattr(observer, "playing_sample", lambda: tuple(sample))

    def play():
        report(music, "loadfile", "confirmed", 4)
        return True

    p._play_async(play, title="新录音")
    wait_until(lambda: "正在加载" in p.lbl_status.text())
    QTest.qWait(210)
    sample[:] = [8, 0]
    QTest.qWait(320)
    assert "正在加载" in p.lbl_status.text()
    sample[1] = 100.0
    QTest.qWait(250)
    assert "正在加载" in p.lbl_status.text()
    sample[1] = 100.2
    wait_until(lambda: p.lbl_status.text() == "已开始播放：新录音")


@pytest.mark.parametrize("reply,state", [(None, "unknown"), ({"error":"failure"}, "failed"),
                                         ({"error":"success"}, "accepted")])
def test_history_keeps_selection_until_confirmed(scene, monkeypatch, reply, state):
    p, movie = scene.panel, scene.controller
    dialog = QDialog(p)
    dialog.playback_status = QLabel(dialog)
    dialog.show()
    monkeypatch.setattr(movie, "history_item", lambda row: scene.item)
    scene.mpv.loadfile.return_value = reply
    assert p._play_movie_history_row({"id": 8}, dialog)
    assert "正在加载" in dialog.playback_status.text()
    assert not scene.mpv.loadfile.called
    wait_until(lambda: scene.mpv.loadfile.called)
    assert dialog.isVisible()
    assert dialog.playback_status.text() == p.lbl_status.text()
    if state == "accepted":
        assert "正在加载" in dialog.playback_status.text()
        gen = movie._playback_gen
        p._on_playback_status(gen - 1, "PROGRESS", "旧片")
        p._on_playback_status(gen, "PROGRESS", "过早")
        assert dialog.isVisible() and "正在加载" in dialog.playback_status.text()
        movie._startup = replace(movie.playback_startup(), state="started")
        p._on_playback_status(gen, "PROGRESS", "已确认播放")
        assert dialog.result() == QDialog.Accepted
    else:
        assert ("未确认" if state == "unknown" else "暂时无法播放") in dialog.playback_status.text()
        assert dialog.result() != QDialog.Accepted
        dialog.reject()
    scene.mpv.loadfile.assert_called_once()

def test_accepted_without_progress_expires_without_a_second_load(panel, monkeypatch):
    _, music, _ = panel
    music._running = True
    music._play_request_id = 2
    music._movie_permits_request = lambda *_: True
    observer = music._next_feedback = NextFeedback(music)
    monkeypatch.setattr(observer, "_property", lambda _: [])
    monkeypatch.setattr(observer, "playing_sample", lambda: (1, 0.0))
    play = Mock(side_effect=lambda: report(music, "loadfile", "confirmed", 2))
    stages = []
    observe_manual_play(music, play, (), Event(), lambda stage, epoch: stages.append(stage), budget=.2)
    assert stages == ["accepted", "unknown"]
    play.assert_called_once_with()

def test_history_explicit_retry_uses_the_same_entry(scene, monkeypatch):
    p, movie = scene.panel, scene.controller
    dlg = QDialog(p)
    dlg.playback_status = QLabel(dlg)
    dlg.show()
    monkeypatch.setattr(movie, "history_item", lambda row: scene.item)
    scene.mpv.loadfile.side_effect = [{"error": "failure"}, {"error": "success"}]
    row = {"id": 9}
    p._play_movie_history_row(row, dlg)
    wait_until(lambda: "暂时无法播放" in dlg.playback_status.text())
    assert dlg.isVisible() and scene.mpv.loadfile.call_count == 1
    p._play_movie_history_row(row, dlg)
    wait_until(lambda: "正在加载" in dlg.playback_status.text())
    a, b = scene.mpv.loadfile.call_args_list
    assert a == b
    dlg.reject()


def test_close_before_deferred_history_load(scene, monkeypatch):
    dlg = QDialog(scene.panel)
    dlg.playback_status = QLabel(dlg)
    monkeypatch.setattr(scene.controller, "history_item", lambda row: scene.item)
    scene.panel._play_movie_history_row({"id": 10}, dlg)
    scene.panel._on_app_quit()
    QTest.qWait(20)
    scene.mpv.loadfile.assert_not_called()
