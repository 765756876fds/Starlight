"""Palette and preference contracts, with no user configuration or real devices."""
import json

import pytest

from app.ui.theme.colors import PALETTES
from app.ui.theme.styles import render_style, style_template


def contrast(a, b):
    def luminance(value):
        components = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in components]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    x, y = sorted((luminance(a), luminance(b)))
    return (y + 0.05) / (x + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("text", ["primary_text", "secondary_text"])
@pytest.mark.parametrize("surface", ["window_background", "panel_background", "elevated_background"])
def test_body_and_secondary_text_meet_aa(theme, text, surface):
    palette = PALETTES[theme]
    assert contrast(getattr(palette, text), getattr(palette, surface)) >= 4.5


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_palette_has_all_required_semantics(theme):
    palette = PALETTES[theme]
    for role in ("window_background", "panel_background", "elevated_background", "primary_text",
                 "secondary_text", "border", "accent", "success", "warning", "error", "overlay_background"):
        assert len(getattr(palette, role)) == 7
    assert len({palette.window_background, palette.panel_background, palette.elevated_background}) == 3


def test_semantic_inline_style_round_trip_keeps_alpha_and_independent_colors():
    raw = "color:#e0e0e8; background:rgba(42, 42, 62, 188); border-color:#ab12cd; selection-color:#ffffff;"
    template = style_template(raw, "dark")
    light = render_style(template, "light")
    assert PALETTES["light"].primary_text in light
    assert "rgba(250, 248, 244, 188)" in light
    assert "#ab12cd" in light and "#ffffff" in light
    assert "@theme" not in light
    assert render_style(template, "dark").replace(" ", "") == raw.replace(" ", "")


def test_theme_save_creates_directory_and_preserves_other_preferences(tmp_path, monkeypatch):
    from app.ui import settings_store as store
    path = tmp_path / "profile" / "ui_prefs.json"
    monkeypatch.setattr(store, "UI_PREFS_PATH", str(path))
    assert store.load_ui_prefs()["theme"] == "dark"
    assert store.save_ui_prefs({"theme": "light"})
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(equip_notify=False, movie_content_filter="短剧", future_option={"enabled": True})
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.save_ui_prefs({"theme": "dark"})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["theme"] == "dark"
    assert saved["equip_notify"] is False and saved["movie_content_filter"] == "短剧"
    assert saved["future_option"] == {"enabled": True}


@pytest.mark.parametrize("invalid", [None, "system", 4, []])
def test_invalid_saved_theme_falls_back_without_rewriting_file(tmp_path, monkeypatch, invalid):
    from app.ui import settings_store as store
    path = tmp_path / "prefs.json"
    path.write_text(json.dumps({"theme": invalid, "equip_notify": False}), encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(store, "UI_PREFS_PATH", str(path))
    assert store.load_ui_prefs()["theme"] == "dark"
    assert path.read_bytes() == original


def test_invalid_theme_save_does_not_damage_file(tmp_path, monkeypatch):
    from app.ui import settings_store as store
    path = tmp_path / "prefs.json"
    path.write_text('{"theme":"light","future_option":42}', encoding="utf-8")
    monkeypatch.setattr(store, "UI_PREFS_PATH", str(path))
    original = path.read_bytes()
    assert store.save_ui_prefs({"theme": "unknown"}) is False
    assert path.read_bytes() == original
