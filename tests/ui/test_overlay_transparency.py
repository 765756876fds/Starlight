"""Transparent game HUD background, with visible and interactive foreground."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from app.ui.overlay import overlay_window
from app.ui.overlay.overlay_model import OverlayEnemyRow, OverlaySpell
from app.ui.theme.manager import get_theme_manager

pytestmark = pytest.mark.ui


@pytest.fixture
def hud(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from app.ui import settings_store

    monkeypatch.setattr(settings_store, "UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(overlay_window, "load_ui_prefs", lambda: {})
    monkeypatch.setattr(
        overlay_window, "QSettings",
        lambda *args: QSettings(str(tmp_path / "overlay.ini"), QSettings.IniFormat),
    )
    image = QPixmap(64, 64)
    image.fill(QColor("#247c85"))
    for name in (
        "get_champion_portrait", "get_summoner_spell_icon_by_sid",
        "get_summoner_spell_icon_by_zh",
    ):
        monkeypatch.setattr(overlay_window, name, lambda *args: QPixmap(image))
    theme = get_theme_manager(app)
    windows = []

    def create(scale, rows):
        monkeypatch.setattr(overlay_window, "load_ui_prefs", lambda: {"overlay_scale": scale})
        model = SimpleNamespace(get_rows=lambda: rows)
        window = overlay_window.OverlayWindow(model)
        window._timer.stop()
        windows.append(window)
        return window

    yield create, theme
    for window in windows:
        window._timer.stop()
        window.close()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    theme.set_theme("dark", persist=False)


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_empty_canvas_is_transparent_after_theme_switch(hud, mode, scale):
    create, theme = hud
    window = create(scale, [])
    before = (window.pos(), window.size(), window._model, window._timer.interval())
    for selected in (mode, "light" if mode == "dark" else "dark", mode):
        theme.set_theme(selected, persist=False)
        image = window.grab().toImage()
        for x, y in ((20, 20), (image.width() // 2, image.height() // 2)):
            assert image.pixelColor(x, y).alpha() == 0
    assert (window.pos(), window.size(), window._model, window._timer.interval()) == before
    assert window.graphicsEffect() is None
    assert window.windowOpacity() == 1.0


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_card_gutter_is_transparent_and_countdown_stays_visible_and_clickable(hud, mode, scale):
    create, theme = hud
    spell = OverlaySpell(1, 4, "闪现", 300, remaining=28, active=True)
    row = OverlayEnemyRow(54, "墨菲特", "TOP", 0, spell, spell)
    window = create(scale, [row])
    window._refresh()
    window._timer.stop()
    card = window._cards[0]
    theme.set_theme(mode, persist=False)
    assert window._cards[0] is card
    assert card.spell1_icon._spell.remaining == 28
    image = card.grab().toImage()
    assert image.pixelColor(20, image.height() // 2).alpha() == 0
    foreground = card.spell1_icon.grab().toImage()
    white_pixels = sum(
        1 for y in range(foreground.height()) for x in range(foreground.width())
        if (color := foreground.pixelColor(x, y)).alpha() > 200
        and min(color.red(), color.green(), color.blue()) > 220
    )
    assert white_pixels > 10, "The actual painted countdown must remain visible"
    assert not card.avatar.pixmap().isNull()
    signal = QSignalSpy(window.spell_clicked)
    QTest.mouseClick(card.spell1_icon, Qt.LeftButton)
    assert signal.count() == 1
    assert signal.at(0) == ["墨菲特", "闪现"]


def test_native_window_and_interaction_flags_are_preserved(hud):
    create, _ = hud
    window = create(1.0, [])
    flags = window.windowFlags()
    for flag in (Qt.FramelessWindowHint, Qt.WindowStaysOnTopHint, Qt.WindowDoesNotAcceptFocus):
        assert flags & flag
    assert flags & Qt.WindowType_Mask == Qt.Tool
    assert window.testAttribute(Qt.WA_TranslucentBackground)
    assert window.testAttribute(Qt.WA_NoSystemBackground)
    assert window.testAttribute(Qt.WA_ShowWithoutActivating)
    assert not window.testAttribute(Qt.WA_TransparentForMouseEvents)
    assert not flags & Qt.WindowTransparentForInput
    assert window.windowOpacity() == 1.0
