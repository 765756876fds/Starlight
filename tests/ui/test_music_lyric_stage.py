"""Playback-state and rendering contracts for the formal Qt lyrics view."""
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest

from app.ui import settings_store
from tests.ui.test_download_lyrics_experience import prefs
from tests.ui.test_music_search_reliability import panel, qapp

pytestmark = pytest.mark.ui


def test_three_stages_follow_one_clock_and_persist_selection(panel, prefs, monkeypatch):
    p, music, errors = panel
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)
    music.is_playing = Mock(return_value=True)
    music.current_playback_time = 4.1
    p.resize(850, 650)
    p.show()
    QTest.mouseClick(p.btn_playing_lyrics, Qt.LeftButton)
    engine, view = p._lyrics_window, p.playing_lyrics
    engine._timer.stop()
    engine.current_track = {"title": "星河", "artist": "乐团", "key": "local:1"}
    engine.set_lyrics([(0., "沿水而行"), (3., "Into the blue"), (6., "天光")], "星河")
    art = QPixmap(60, 60)
    art.fill(QColor("#329f90"))
    view._apply_cover(art)
    assert view.pages.currentWidget() is view.stage and view.stage.style_index == 0
    rendered = []
    for i, name in enumerate(("space", "flow", "cinema")):
        view.style_actions[name].trigger()
        QTest.qWait(45)
        assert view.stage.style_index == i and view.stage.row == 1
        assert view.stage.accessibleDescription() == "Into the blue"
        assert settings_store.load_lyrics_prefs()["stage_style"] == name
        rendered.append(view.stage.grab().toImage())
    assert rendered[0] != rendered[1] and rendered[1] != rendered[2]
    view.reader_action.trigger()
    assert view.pages.currentWidget() is view.reader
    view.btn_close.click()
    assert not view.stage.timer.isActive()
    p.btn_playing_lyrics.click()
    assert view.pages.currentWidget() is view.stage and view.stage.style_index == 2
    assert not errors


def test_pause_seek_offset_hidden_and_old_meter_samples(panel, prefs, monkeypatch):
    p, music, errors = panel
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)
    music.is_playing = Mock(return_value=True)
    music.current_playback_time = 4.
    p.show()
    p.btn_playing_lyrics.click()
    engine, stage = p._lyrics_window, p.playing_lyrics.stage
    engine._timer.stop()
    track = {"key": "fixture:new", "title": "听海"}
    engine.current_track = track
    engine.set_lyrics([(1., "海"), (3.7, "月"), (7., "舟")], "听海")
    assert stage.row == 1 and stage.timer.isActive()
    request = p._meter_request
    stamp = time.monotonic()
    p._on_meter_sample(request, (.4, .6), stamp, 4.)
    assert stage.levels == (.4, .6)
    p._on_meter_sample(object(), (.8, 1.), stamp, 4.)
    p._on_meter_sample(request, (.8, 1.), stamp, 10.)
    stage.accept_levels("fixture:old", (.8, 1.), stamp)
    stage.accept_levels(track["key"], (.8, 1.), stamp - 2.)
    assert stage.levels == (.4, .6)
    engine._prefs["offset_ms"] = 700
    stage.sync()
    assert stage.row == 0
    music.current_playback_time = .1
    stage.sync()
    assert stage.row == -1
    music.is_playing.return_value = False
    stage.sync()
    assert not stage.timer.isActive() and stage._meter_key is None
    frozen = stage.grab().toImage()
    QTest.qWait(80)
    assert stage.grab().toImage() == frozen
    music.current_playback_time = 8.
    stage.sync()
    assert stage.row == 2
    music.is_playing.return_value = True
    p._change_lyrics_prefs({"stage_low_power": True})
    assert stage.timer.interval() == 33
    p._change_lyrics_prefs({"reduce_motion": True})
    assert not stage.timer.isActive() and not p._meter_request
    p.btn_playing_lyrics.click()
    assert not stage.isVisible() and not stage.timer.isActive()
    assert not errors


def test_changed_song_discards_a_visible_transition_snapshot(panel, prefs, monkeypatch):
    p, music, errors = panel
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)
    music.is_playing = Mock(return_value=True)
    music.current_playback_time = .2
    music.get_current_song = Mock(return_value={"path": "A.wav", "id": 1, "title": "第一首"})
    p.show()
    p.btn_playing_lyrics.click()
    QTest.qWait(50)
    stack = p._content_pages
    assert stack._outgoing.isVisible()
    music.get_current_song.return_value = {"path": "B.wav", "id": 2, "title": "第二首"}
    p._lyrics_window._request_lyric(music.get_current_song())
    assert p.playing_lyrics.title.text() == "第二首"
    assert not stack._outgoing.isVisible() and p.playing_lyrics.stage.reveal == 1.
    assert not errors


def test_untimed_reader_does_not_invent_a_playing_row(panel, prefs, monkeypatch):
    from app.music.lyrics import parse_document
    p, music, errors = panel
    music.is_playing = Mock(return_value=True)
    music.current_playback_time = 9.
    p.show()
    p.btn_playing_lyrics.click()
    view, engine = p.playing_lyrics, p._lyrics_window
    engine._timer.stop()
    engine.current_track = {"key": "plain", "title": "晓"}
    engine.set_lyrics(parse_document("第一段\nStill here"), "晓")
    assert engine.line_at(9.) == -1 and engine.lyric_status == "plain"
    assert view.pages.currentWidget() is view.reader and view.lines.count() == 2
    assert not view.follow.isVisible() and not view.stage.timer.isActive()
    engine.set_lyrics(parse_document("[00:01]<0,230>新<600,90>词"), "新歌")
    assert view.pages.currentWidget() is view.stage
    assert view.stage.lines == [(1., "新词")] and engine._word_cues[0][1].start == 1.6
    assert not errors


def test_projected_hero_moves_after_entrance_and_word_layout_survives_wrap(panel, prefs, monkeypatch):
    from app.music.lyrics import parse_document
    from app.ui.widgets.lyric_stage import Light
    p, music, errors = panel
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)
    music.is_playing = Mock(return_value=False)
    music.current_playback_time = 4.
    p.resize(850, 680)
    p.show()
    p.btn_playing_lyrics.click()
    e, s = p._lyrics_window, p.playing_lyrics.stage
    e._timer.stop()
    e.current_track = {"key": "art", "title": "晓"}
    e.set_lyrics(parse_document("[00:00]<0,535>🌙 skyline <2000,5500>跨过银色海面，让夜空亮起来"), "晓")
    glyph = s._glyph(0)
    assert glyph.rows > 1 and len(glyph.word_boxes) == 2
    assert len(glyph.word_boxes[1]) >= 2
    assert all(box.width() > 0 and box.height() > 0 for group in glyph.word_boxes for box in group)
    light = Light(.5, .7, .2, 0.)
    a, b = s._hero_pose(0, 3., light), s._hero_pose(0, 4.2, light)
    assert abs(a.yaw) > 4 and abs(b.roll) > 1
    assert abs(a.x-b.x) + abs(a.y-b.y) + abs(a.z-b.z) > 8
    img = s.grab().toImage()
    music.current_playback_time = 6.
    s.sync()
    assert s.grab().toImage() != img and s._glyph(0) is glyph
    e.set_lyrics(parse_document("[00:00]同一行只按句同步"), "新歌")
    assert not s._glyph(0).word_boxes and not e._word_cues
    assert not errors


@pytest.mark.parametrize("style", ["space", "flow", "cinema"])
def test_each_stage_lights_measured_onsets_in_reduced_motion(panel, prefs, style):
    from app.music.lyrics import parse_document
    p, music, errors = panel
    music.is_playing = Mock(return_value=False)
    music.current_playback_time = .1
    # Use a supported content width, independent of the toolbar's size hint.
    p.resize(850, 650)
    p.show()
    p.btn_playing_lyrics.click()
    engine, view = p._lyrics_window, p.playing_lyrics
    engine._timer.stop()
    engine.current_track = {"key": "owned", "title": "星光"}
    engine.set_lyrics(parse_document("[00:00]<600,200>星<1700,240>光"), "星光")
    p._change_lyrics_prefs({"reduce_motion": True})
    view.style_actions[style].trigger()
    first = view.stage.grab().toImage()
    music.current_playback_time = .7
    view.stage.sync()
    active = view.stage.grab().toImage()
    music.current_playback_time = 1.
    view.stage.sync()
    assert active == view.stage.grab().toImage()
    music.current_playback_time = 1.8
    view.stage.sync()
    assert first != active != view.stage.grab().toImage()
    assert not view.stage.timer.isActive() and not errors


def test_unavailable_graphics_context_uses_the_same_raster_scene(panel, prefs, monkeypatch):
    from PySide6.QtGui import QOpenGLContext
    from app.ui.widgets.lyric_stage_surface import StageSurface

    p, music, errors = panel
    music.is_playing = Mock(return_value=False)
    music.current_playback_time = 2.
    p.show()
    p.btn_playing_lyrics.click()
    view = p.playing_lyrics
    stage = view.stage
    view.engine._timer.stop()
    view.engine.current_track = {"title": "晚风", "key": "fixture:surface"}
    view.engine.set_lyrics([(0., "晚风"), (4., "夜色渐深")], "晚风")
    stage.configure(0, 0, "Microsoft YaHei UI", 24)
    stage.sync()
    monkeypatch.setattr(stage, "_surface", None)
    expected = stage.grab().toImage()
    previous = QOpenGLContext.currentContext()
    compositor = StageSurface(stage)
    unavailable = Mock(return_value=False)
    monkeypatch.setattr(compositor, "_open", unavailable)
    monkeypatch.setattr(stage, "_surface", compositor)
    assert stage.grab().toImage() == expected
    assert stage.grab().toImage() == expected
    unavailable.assert_called_once()
    assert compositor.unavailable and not compositor.retire.isActive()
    assert QOpenGLContext.currentContext() is previous
    assert not errors


def test_stage_shutdown_after_qt_has_destroyed_the_native_surface(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QOffscreenSurface
    from PySide6.QtWidgets import QWidget
    from shiboken6 import isValid
    from app.ui.widgets.lyric_stage_surface import StageSurface

    owner = QWidget()
    compositor = StageSurface(owner)
    native = QOffscreenSurface()
    compositor.surface = native
    native.deleteLater()
    QCoreApplication.sendPostedEvents(native, QEvent.DeferredDelete)
    assert not isValid(native)
    compositor.close()
    compositor.close()
    assert compositor.context is None and compositor.surface is None
    owner.deleteLater()


def test_text_effect_selection_survives_stage_change_and_return(panel, prefs, monkeypatch):
    p, music, errors = panel
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)
    p.resize(850, 650)
    p.show()
    p.btn_playing_lyrics.click()
    view = p.playing_lyrics
    assert view.stage.effect == "starlight" and view.effect_actions["starlight"].isChecked()

    view.effect_actions["ice"].trigger()
    view.style_actions["cinema"].trigger()
    assert view.stage.style_index == 2 and view.stage.effect == "ice"
    assert settings_store.load_lyrics_prefs()["stage_effect"] == "ice"
    view.reader_action.trigger()
    assert view.effect_button.isHidden()
    p.btn_playing_lyrics.click()
    p.btn_playing_lyrics.click()
    assert view.stage.effect == "ice" and view.effect_button.isVisible()

    view.effect_actions["off"].trigger()
    assert settings_store.load_lyrics_prefs()["stage_effect"] == "off"
    assert [key for key, action in view.effect_actions.items() if action.isChecked()] == ["off"]
    p._change_lyrics_prefs({"stage_effect": ["invalid"]})
    assert view.stage.effect == "starlight" and not errors


def test_text_effect_uses_only_real_word_duration_and_current_line(panel, prefs, monkeypatch):
    from app.music.lyrics import parse_document

    p, music, errors = panel
    monkeypatch.setattr("app.ui.widgets.playing_lyrics.reduced_motion_enabled", lambda: False)
    music.is_playing = Mock(return_value=False)
    music.current_playback_time = 0.4
    p.show()
    p.btn_playing_lyrics.click()
    engine, stage = p._lyrics_window, p.playing_lyrics.stage
    engine._timer.stop()
    engine.current_track = {"title": "夜", "key": "fixture:ink"}
    text = "[00:00]<600,400>星<1700,200>光\n[00:03]照亮夜空"
    engine.set_lyrics(parse_document(text), "夜")
    draw = Mock()
    monkeypatch.setattr(stage._effects, "draw", draw)

    def patches_at(second):
        draw.reset_mock()
        music.current_playback_time = second
        stage.sync()
        stage.grab()
        assert draw.call_count >= 1
        for call in draw.call_args_list:
            assert call.args[-1].row == stage.row
        return draw.call_args.args[4]

    assert not patches_at(.5)
    assert patches_at(.7)[0].progress == pytest.approx(.25)
    assert patches_at(.9)[0].progress == pytest.approx(.75)
    assert not patches_at(1.4)
    assert patches_at(1.8)[0].progress == pytest.approx(.5)
    assert patches_at(3.7)[0].progress is None
    p._change_lyrics_prefs({"reduce_motion": True})
    draw.reset_mock()
    stage.grab()
    draw.assert_not_called()
    assert not stage.timer.isActive() and not errors


def test_album_landscape_keeps_spatial_detail_and_tames_bright_covers(qapp):
    from PySide6.QtGui import QPainter
    from app.ui.widgets.lyric_stage_art import album_landscape

    cover = QPixmap(480, 480)
    cover.fill(QColor("#ffffff"))
    with QPainter(cover) as painter:
        painter.fillRect(240, 0, 240, 480, QColor("#263550"))
    image = album_landscape(cover).toImage()
    bright = image.pixelColor(100, 100)
    dark = image.pixelColor(image.width() - 100, 100)
    assert 45 < bright.lightness() < 120
    assert bright.lightness() - dark.lightness() > 40


def test_glow_cleanup_after_context_destroyed(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
    from PySide6.QtWidgets import QWidget
    from shiboken6 import isValid
    from app.ui.widgets.lyric_stage_surface import StageSurface

    owner = QWidget()
    compositor = StageSurface(owner)
    context = QOpenGLContext()
    compositor.context = context
    compositor.surface = QOffscreenSurface()
    glow = Mock()
    compositor.glow = glow
    context.deleteLater()
    QCoreApplication.sendPostedEvents(context, QEvent.DeferredDelete)
    assert not isValid(context)
    compositor.close()
    compositor.close()
    glow.release.assert_not_called()
    assert compositor.context is None and compositor.glow is None
    owner.deleteLater()


@pytest.mark.parametrize("quality", [0, 1, 2])
def test_cinema_has_one_readable_lead_at_each_side_of_the_cue(quality):
    from app.ui.widgets.lyric_stage import LyricStage

    draw = Mock()
    stage = SimpleNamespace(engine=SimpleNamespace(_lines=[(0., "上一句"), (2., "正在唱"), (4., "下一句")]),
                            quality=quality, _flat_glyph=draw)
    for row, second in [(0, 1.96), (1, 2.), (1, 2.08), (1, 2.36), (1, 2.72)]:
        draw.reset_mock()
        LyricStage._cinema(stage, None, second, row, 1.)
        main = [c for c in draw.call_args_list if c.args[2].y() < 340 and c.args[4] > .01]
        assert [c.args[1] for c in main] == [row]
        assert main[0].kwargs.get("current") and main[0].args[4] >= .35
