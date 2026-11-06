"""Real Qt theme changes; media, voice and game services are never started."""
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QThread, QTimer, QCoreApplication, QEvent
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget, QVBoxLayout

from app.ui.theme.colors import PALETTES, Colors
from app.ui.theme.manager import get_theme_manager
from app.ui.theme.tokens import Tokens


@pytest.fixture
def theme(tmp_path, monkeypatch):
    from app.ui import settings_store as store
    monkeypatch.setattr(store, "UI_PREFS_PATH", str(tmp_path / "config" / "prefs.json"))
    app = QApplication.instance() or QApplication([])
    manager = get_theme_manager(app)
    manager.set_theme("dark", persist=False, force=True)
    yield manager
    manager.set_theme("dark", persist=False)
    QTest.qWait(10)


def test_both_palettes_and_derived_tokens_apply_to_existing_widgets(theme):
    widget = QLabel("已连接")
    widget.setStyleSheet(f"color:{Tokens.SUCCESS}; background:{Tokens.BG_ELEVATED};")
    for mode in ("light", "dark", "light"):
        theme.set_theme(mode, persist=False)
        QTest.qWait(10)
        assert widget.palette().color(QPalette.Window).name() == PALETTES[mode].elevated_background
        assert Tokens.TEXT_PRIMARY == PALETTES[mode].primary_text
        assert Tokens.BG_BASE == Colors.BG_BASE
        assert PALETTES[mode].success in widget.styleSheet()
        assert "@theme" not in widget.styleSheet()
    widget.close()


def test_settings_and_top_bar_switch_each_other_without_recreating_widgets(theme):
    from app.ui.panels.settings_panel import SettingsPanel
    from app.ui.widgets.title_bar import GlassTitleBar
    panel = SettingsPanel()
    title = GlassTitleBar("星芒")
    identity = id(panel.cmb_theme)
    panel.cmb_theme.setCurrentIndex(panel.cmb_theme.findData("light"))
    assert theme.mode == "light" and title.btn_theme.text() == "夜间"
    title.btn_theme.click()
    assert theme.mode == "dark" and panel.cmb_theme.currentData() == "dark"
    assert id(panel.cmb_theme) == identity
    assert panel.lbl_theme_status.text()
    panel.close()
    title.close()


def test_preference_is_restored_in_a_fresh_process(theme, tmp_path):
    import os
    import subprocess
    import sys
    from app.ui import settings_store as store
    assert theme.set_theme("light")
    code = ("from PySide6.QtWidgets import QApplication; "
            "from app.ui import settings_store as s; "
            f"s.UI_PREFS_PATH={store.UI_PREFS_PATH!r}; "
            "from app.ui.theme.manager import get_theme_manager; "
            "a=QApplication([]); m=get_theme_manager(a); print('RESTORED='+m.mode)")
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"RESTORED=light" in result.stdout


def test_notification_and_island_recolor_without_changing_history_or_timers(theme):
    from app.core.notifications import NotificationCenter
    from app.ui.panels.notification_panel import NotificationPanel
    from app.ui.widgets.voice_island import VoiceIsland
    center = NotificationCenter()
    key = center.post("音乐已暂停", heard="暂停音乐", status="success", source="voice", phase="done")
    center.update(key, speech_status="failed")
    panel = NotificationPanel(center=center)
    island = VoiceIsland()
    island.set_current(center.get(key))
    record = center.history()
    title = island.text()
    theme.set_theme("light", persist=False)
    assert center.history() == record
    assert panel.view.itemDelegate().style.text == PALETTES["light"].primary_text
    assert island.colors.surface == PALETTES["light"].elevated_background
    assert island.text() == title and island.timer.isActive()
    panel.close()
    island.close()


def test_queue_snapshot_and_random_state_are_not_modified(theme):
    from app.music.queue import PlayQueue, PlayMode
    from app.ui.widgets.music_queue_dialog import MusicQueueDialog
    queue = PlayQueue()
    queue.add_all([{"title": t, "artist": "测试歌手", "path": t + ".mp3"} for t in "ABC"])
    queue.set_play_mode(PlayMode.SHUFFLE)
    queue.play_at(0)
    queue.add_next({"title": "D", "path": "D.mp3"})
    snapshot = dict(queue.snapshot(queue.get_current()), state="playing",
                    current=queue.song_summary(queue.get_current()))
    dialog = MusicQueueDialog()
    dialog.set_snapshot(snapshot)
    def state():
        return copy.deepcopy((queue._queue, queue._current_index, queue._history,
                              queue._history_pos, queue._shuffle_order, queue._shuffle_pos,
                              queue._next_up, queue._play_mode))
    before = state()
    model_id = id(dialog.other_model)
    theme.set_theme("light", persist=False)
    assert state() == before
    assert id(dialog.other_model) == model_id
    assert PALETTES["light"].elevated_background in dialog.styleSheet()
    assert [row["title"] for row in dialog.next_model.rows] == ["D"]
    dialog.close()


def test_movie_canvas_stays_black_and_subtitle_menu_recolors_without_playback(theme):
    from app.ui.widgets.video_player_window import VideoPlayerWindow
    movie = Mock()
    movie.is_paused.return_value = False
    movie.position.return_value = 0
    movie.duration.return_value = 120
    window = VideoPlayerWindow(movie)
    window._timer.stop()
    menu = window._build_subtitle_menu({"media": "test.mp4", "visible": True, "sid": 1,
                                       "tracks": [{"id": 1, "title": "中文", "lang": "zh"}]})
    movie.reset_mock()
    theme.set_theme("light", persist=False)
    assert "#000" in window.container.styleSheet()
    assert PALETTES["light"].elevated_background in menu.styleSheet()
    assert "加载本地字幕" in menu.actions()[0].text()
    movie.play.assert_not_called()
    movie.stop.assert_not_called()
    window.hide()


def test_game_overlay_stays_transparent_without_resetting_model_or_refresh_timer(theme):
    from app.ui.overlay.overlay_window import OverlayWindow
    model = SimpleNamespace(get_rows=lambda: [])
    overlay = OverlayWindow(model)
    timer = overlay._timer
    before = (overlay.pos(), overlay.size(), timer.interval(), overlay._model)
    theme.set_theme("light", persist=False)
    assert overlay.grab().toImage().pixelColor(20, 20).alpha() == 0
    assert (overlay.pos(), overlay.size(), timer.interval(), overlay._model) == before
    overlay._timer.stop()
    overlay.close()


def test_existing_overlay_cards_keep_cooldowns_and_identity_during_switch(theme, monkeypatch):
    from app.ui.overlay import overlay_assets
    from app.ui.overlay.overlay_model import OverlayEnemyRow, OverlaySpell
    from app.ui.overlay.overlay_window import OverlayWindow
    monkeypatch.setattr(overlay_assets, "_download_file", lambda *args: False)
    spell = OverlaySpell(slot=1, spell_id=4, spell_name="闪现", base_cooldown=300,
                         remaining=28, active=True)
    row = OverlayEnemyRow(champion_id=54, champion_name="墨菲特", position="TOP", order=0,
                          spell1=spell, spell2=spell)
    get_rows = Mock(return_value=[row])
    overlay = OverlayWindow(SimpleNamespace(get_rows=get_rows))
    overlay._refresh()
    overlay._timer.stop()
    card = overlay._cards[0]
    before_calls = get_rows.call_count
    theme.set_theme("light", persist=False)
    assert overlay._cards[0] is card
    assert card.spell1_icon._spell.remaining == 28
    assert PALETTES["light"].error in card.spell1_icon.styleSheet()
    assert get_rows.call_count == before_calls
    overlay.close()


@pytest.mark.parametrize("width", [320, 640, 1280])
def test_open_dialogs_and_new_dialogs_share_theme_at_different_widths(theme, width):
    from app.ui.widgets.glass_dialog import GlassDialog
    dialog = GlassDialog("主题检查")
    dialog.resize(width, 300)
    theme.set_theme("light", persist=False)
    later = GlassDialog("稍后打开")
    later.resize(width, 300)
    QTest.qWait(10)
    for widget in (dialog, later):
        assert widget.palette().color(QPalette.WindowText).name() == PALETTES["light"].primary_text
        assert not widget.grab().isNull()
        widget.close()


def test_worker_request_applies_on_gui_thread(theme):
    seen = []
    def changed(mode):
        seen.append((mode, QThread.currentThread()))
    theme.theme_changed.connect(changed)
    try:
        worker = threading.Thread(target=lambda: theme.request_theme("light"))
        worker.start()
        worker.join(1)
        QTest.qWait(20)
        assert seen == [("light", QApplication.instance().thread())]
    finally:
        theme.theme_changed.disconnect(changed)


def test_late_status_style_and_user_color_are_not_confused(theme):
    label = QLabel("状态")
    preview = QPushButton("用户颜色")
    preview.setProperty("themeIndependent", True)
    preview.setStyleSheet("background:#4a9eff;")
    theme.set_theme("light", persist=False)
    label.setStyleSheet("color:#4ade80;")  # Legacy style on a newly opened widget.
    label.show()
    QTest.qWait(10)
    assert PALETTES["light"].success in label.styleSheet()
    assert preview.styleSheet() == "background:#4a9eff;"
    label.close()
    preview.close()


def test_runtime_game_card_refresh_uses_the_current_theme(theme):
    from app.ui.panels.current_game_panel import PlayerCard
    card = PlayerCard(is_ally=True)
    for mode in ("light", "dark", "light"):
        theme.set_theme(mode, persist=False)
        card.update_data("示例玩家", 30, "上单", "", None)
        assert PALETTES[mode].primary_text in card.lbl_name.styleSheet()
        assert PALETTES[mode].primary_text in card.lbl_line2.styleSheet()
        assert PALETTES[mode].accent in card.styleSheet()
    card.close()


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_waveform_remains_visible_in_both_themes_without_changing_position(theme, mode):
    from app.ui.widgets.waveform_slider import WaveformSlider
    theme.set_theme(mode, persist=False)
    holder = QWidget()
    holder.setAutoFillBackground(True)
    layout = QVBoxLayout(holder)
    slider = WaveformSlider()
    slider.setFixedSize(200, 80)
    layout.addWidget(slider)
    holder.show()
    QTest.qWait(10)
    slider._timer.stop()
    slider.setValue(100)
    slider._bars = [0.8] * 30
    image = holder.grab().toImage()
    origin = slider.pos()
    assert image.pixelColor(origin.x() + 61, origin.y() + 28) != image.pixelColor(origin.x() + 64, origin.y() + 28), "未播放的波形柱应区别于相邻柱间空隙"
    assert slider.value() == 100 and slider.maximum() == 1000
    holder.close()


def test_qt_style_replacement_is_not_intercepted_during_native_cleanup(theme, monkeypatch):
    app = QApplication.instance()
    label = QLabel("状态")
    label.show()
    QTest.qWait(10)
    original = app.styleSheet()
    capture = Mock(wraps=theme._capture_style)
    monkeypatch.setattr(theme, "_capture_style", capture)
    assert theme.eventFilter(label, QEvent(QEvent.StyleChange)) is False
    assert capture.call_count == 0, "StyleChange must not inspect native style objects"
    label.setStyleSheet(f"color:{Tokens.SUCCESS};")
    app.setStyleSheet("")
    app.setStyleSheet(original)
    # Other widgets may legitimately be polished during global replacement.
    QTest.qWait(10)
    assert Tokens.SUCCESS in label.styleSheet()
    label.close()


def test_save_failure_is_reported_without_reverting_the_current_theme(theme, monkeypatch):
    from app.ui import settings_store as store
    monkeypatch.setattr(store, "save_ui_prefs", lambda update: False)
    assert theme.set_theme("light") is False
    assert theme.mode == "light"
    assert "未保存" in theme.last_error


def test_closed_widget_is_safe_when_a_queued_style_update_is_pending(theme):
    widget = QLabel("即将关闭")
    widget.setStyleSheet(f"color:{Colors.TEXT_PRIMARY};")
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    theme.set_theme("light", persist=False)
    QTest.qWait(20)
    assert theme.mode == "light"


def test_startup_style_reapplication_preserves_the_selected_theme(theme):
    from app.ui.theme.applier import apply_glass_theme, apply_dark_theme
    theme.set_theme("light", persist=False)
    app = QApplication.instance()
    app.setStyleSheet("")
    apply_glass_theme(app)
    assert theme.mode == "light" and "QTabBar::tab:selected" in app.styleSheet()
    apply_dark_theme(app)
    assert theme.mode == "light"


def test_existing_icon_repaints_in_the_new_theme(theme):
    from app.ui.widgets.icon_utils import lucide_icon
    button = QPushButton()
    button.setIcon(lucide_icon("sun", 24, Tokens.TEXT_PRIMARY))
    def colors():
        image = button.icon().pixmap(24, 24).toImage()
        return {image.pixelColor(x, y).name() for x in range(image.width())
                for y in range(image.height()) if image.pixelColor(x, y).alpha() > 240}
    assert PALETTES["dark"].primary_text in colors()
    theme.set_theme("light", persist=False)
    assert PALETTES["light"].primary_text in colors()
    assert PALETTES["dark"].primary_text not in colors()
    button.close()


@pytest.mark.parametrize("scale", ["1", "1.25", "1.5"])
def test_actual_qt_scaling_keeps_theme_controls_inside_a_narrow_window(theme, scale):
    import subprocess
    import sys
    import os
    code = '''import json
from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout
from PySide6.QtTest import QTest
from app.ui.theme.manager import get_theme_manager
from app.ui.widgets.title_bar import GlassTitleBar
from app.ui.widgets.voice_island import VoiceIsland
a=QApplication([]);m=get_theme_manager(a);m.set_theme("light",persist=False)
w=QWidget();layout=QVBoxLayout(w);title=GlassTitleBar("星芒");island=VoiceIsland()
layout.addWidget(title);layout.addWidget(island);w.resize(360,160);w.show();QTest.qWait(40)
assert title.geometry().right() < w.width()
assert title.btn_theme.geometry().right() < title.width()
assert island.geometry().right() < w.width()
print(json.dumps({"dpr":w.devicePixelRatioF(),"width":w.width(),"image":w.grab().width()}))
w.close()
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, timeout=15,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    data = json.loads(result.stdout.decode("utf-8").splitlines()[-1])
    assert data["dpr"] == pytest.approx(float(scale))
    assert data["image"] == round(data["width"] * float(scale))


def test_main_window_and_five_real_tabs_switch_without_reinitializing_services(theme, monkeypatch):
    from app.ui.main_window import MainWindow
    from app.ui.widgets.main_tabs import MainTabs
    from app.ui.panels.music_panel import MusicPanel
    from app.ui.panels.movie_panel import MoviePanel
    monkeypatch.setattr("subprocess.Popen", Mock(side_effect=AssertionError("No helper process expected")))
    tabs = MainTabs(None, None, None)
    assert tabs.tabs.count() == 5
    assert isinstance(tabs._panels["music"], MusicPanel)
    assert isinstance(tabs._panels["movie"], MoviePanel)
    window = MainWindow(music=None)
    window.switch_to_tabs(tabs)
    identities = [id(tabs.tabs.widget(i)) for i in range(5)]
    timers = [(timer, timer.interval(), timer.isActive()) for timer in tabs.findChildren(QTimer)]
    theme.set_theme("light", persist=False)
    QTest.qWait(20)
    assert [id(tabs.tabs.widget(i)) for i in range(5)] == identities
    for index in range(5):
        assert tabs.tabs.widget(index).palette().color(QPalette.WindowText).name() == PALETTES["light"].primary_text
    assert all((timer.interval(), timer.isActive()) == (interval, active) for timer, interval, active in timers)
    window.close()
    for timer, _, _ in timers:
        timer.stop()
