"""Exercise the compact bar with real Qt controls and isolated music services."""
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.ui.test_music_like_button import panel as panel, set_song, wait_for

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("width", [760, 1100])
def test_transport_fits_in_one_row_and_title_elides_when_needed(panel, width):
    widget, ctrl, _ = panel
    widget.resize(width, 640)
    QApplication.processEvents()
    controls = [widget.btn_mode, widget.btn_prev, widget.btn_toggle, widget.btn_next,
                widget.btn_more, widget.btn_volume]
    assert max(button.geometry().center().y() for button in controls) - min(
        button.geometry().center().y() for button in controls) <= 1
    assert all(a.geometry().right() < b.geometry().left() for a, b in zip(controls, controls[1:]))
    assert controls[-1].geometry().right() < widget.width()

    title = "长歌名与现场录音" * 15
    set_song(ctrl, {"id": 31, "title": title})
    widget._refresh()
    QApplication.processEvents()
    assert widget.lbl_track.text().endswith("…")
    assert widget.lbl_track.toolTip() == title
    assert widget.lbl_track.accessibleName() == title
    assert widget.lbl_track.geometry().right() < widget.btn_like.geometry().left()
    wait_for(widget.btn_like.isEnabled)

    set_song(ctrl, {"id": 32, "title": "新曲目"})
    widget._refresh()
    QApplication.processEvents()
    assert widget.lbl_track.text() == "新曲目"
    assert widget.lbl_track.toolTip() == ""


def test_direct_mode_selection_and_more_menu_follow_controller(panel, monkeypatch):
    widget, ctrl, _ = panel
    monkeypatch.setattr("app.ui.settings_store.save_settings_yaml", Mock(return_value=True))
    movie_pauser = Mock(return_value=True)
    ctrl.set_movie_pauser(movie_pauser)
    ctrl.queue.add({"id": 91, "title": "保留在队列"})
    assert widget.btn_mode.menu() is widget._mode_menu
    ctrl.set_play_mode("shuffle")
    widget._mode_menu.popup(widget.btn_mode.mapToGlobal(widget.btn_mode.rect().bottomLeft()))
    QApplication.processEvents()
    assert widget.btn_mode.text() == "随机"
    assert widget._mode_actions.checkedAction().data() == "shuffle"
    target = next(a for a in widget._mode_actions.actions() if a.data() == "repeat_one")
    QTest.mouseClick(widget._mode_menu, Qt.LeftButton,
                     pos=widget._mode_menu.actionGeometry(target).center())
    assert ctrl.get_play_mode() == "repeat_one"
    assert widget.btn_mode.text() == "单曲循环"
    assert widget._mode_actions.checkedAction() is target

    ctrl.set_highlight_mode(True)
    ctrl._speed = 1.25  # Backend-confirmed external speed.
    assert ctrl.set_sleep_timer(3600)
    try:
        assert widget.btn_more.menu() is widget._more_menu
        widget._more_menu.popup(widget.btn_more.mapToGlobal(widget.btn_more.rect().bottomLeft()))
        QApplication.processEvents()
        assert widget.action_highlight.isChecked()
        assert widget._speed_menu.title() == "倍速 1.25x"
        assert widget._speed_actions.checkedAction().text() == "1.25x"
        assert widget._sleep_menu.title() == "睡眠定时 · 已设 60 分钟"
        QTest.mouseClick(widget._more_menu, Qt.LeftButton,
                         pos=widget._more_menu.actionGeometry(widget.action_highlight).center())
        assert not ctrl.is_highlight_mode()
        assert not widget.action_highlight.isChecked()
        widget._sleep_menu.actions()[0].trigger()
        assert widget._sleep_menu.title() == "睡眠定时 · 已设 30 分钟"
        widget._sleep_menu.actions()[-1].trigger()
        assert widget._sleep_menu.title() == "睡眠定时"
    finally:
        ctrl.cancel_sleep_timer()
    assert ctrl.queue.size() == 1
    movie_pauser.assert_not_called()
    widget._more_menu.popup(widget.btn_more.mapToGlobal(widget.btn_more.rect().bottomLeft()))
    QApplication.processEvents()
    assert widget._more_menu.isVisible()
    assert widget.close()
    QApplication.processEvents()
    assert not widget._more_menu.isVisible()


def test_playback_actions_keep_the_existing_routes(panel, monkeypatch):
    widget, ctrl, mpv = panel
    transport = {name: Mock() for name in ("prev", "next", "toggle", "seek", "set_volume", "clear_queue")}
    for name, method in transport.items():
        monkeypatch.setattr(ctrl, name, method)
    playing = Mock(side_effect=[False, True, True])
    monkeypatch.setattr(ctrl, "is_playing", playing)
    widget._timer.stop()
    widget._sync_toggle_icon()

    for key, button in (("prev", widget.btn_prev), ("next", widget.btn_next),
                        ("toggle", widget.btn_toggle)):
        QTest.mouseClick(button, Qt.LeftButton)
        wait_for(lambda: transport[key].called)
        transport[key].assert_called_once_with()
    wait_for(lambda: not widget._icon_locked)
    mpv.get_duration.return_value = 100
    widget.slider_pos.setValue(300)
    widget.slider_pos.sliderReleased.emit()
    wait_for(lambda: transport["seek"].called)
    transport["seek"].assert_called_once_with(30.0, mode="absolute")
    widget.slider_vol.setValue(42)
    wait_for(lambda: transport["set_volume"].called)
    transport["set_volume"].assert_called_once_with(42)

    mark = Mock(return_value=True)
    monkeypatch.setattr(ctrl, "mark_highlight_start", mark)
    next(a for a in widget._more_menu.actions() if a.text() == "标记高潮起点").trigger()
    mark.assert_called_once_with()

    clear = widget._more_menu.actions()[-1]
    prompt = Mock(return_value=QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question", prompt)
    clear.trigger()
    transport["clear_queue"].assert_not_called()
    prompt.return_value = QMessageBox.StandardButton.Yes
    clear.trigger()
    wait_for(lambda: transport["clear_queue"].called)
    transport["clear_queue"].assert_called_once_with()
