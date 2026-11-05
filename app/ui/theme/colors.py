from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    window_background: str
    panel_background: str
    elevated_background: str
    hover_background: str
    active_background: str
    primary_text: str
    secondary_text: str
    disabled_text: str
    border: str
    border_light: str
    accent: str
    accent_hover: str
    accent_pressed: str
    success: str
    warning: str
    warning_light: str
    error: str
    info: str
    overlay_background: str


PALETTES = {
    "dark": ThemePalette(
        "#16161e", "#1e1e2e", "#2a2a3e", "#33334a", "#3e3e5c",
        "#e0e0e8", "#a8a8c0", "#5a5a72", "#3a3a4e", "#4a4a62",
        "#4a9eff", "#5eaaff", "#3a8eef", "#4ade80", "#f59e0b",
        "#ffcc80", "#ef4444", "#06b6d4", "#222232"),
    "light": ThemePalette(
        "#f3f0ea", "#eeece7", "#faf8f4", "#e3e7ec", "#d7e3f1",
        "#242c38", "#536174", "#89929f", "#c9cdd3", "#b2bac6",
        "#2468b8", "#1d5ca7", "#184b8b", "#187346", "#8f5c08",
        "#79500b", "#b53643", "#176d85", "#f4f1eb"),
}

# Existing widgets keep importing Colors; these aliases now follow one palette.
COLOR_ROLES = {
    "BG_DEEPEST": "window_background", "BG_BASE": "panel_background",
    "BG_ELEVATED": "elevated_background", "BG_HOVER": "hover_background",
    "BG_ACTIVE": "active_background", "TEXT_PRIMARY": "primary_text",
    "TEXT_SECONDARY": "secondary_text", "TEXT_DISABLED": "disabled_text",
    "BORDER": "border", "BORDER_LIGHT": "border_light", "ACCENT": "accent",
    "ACCENT_HOVER": "accent_hover", "ACCENT_PRESSED": "accent_pressed",
    "SUCCESS": "success", "WARNING": "warning", "WARNING_LIGHT": "warning_light",
    "DANGER": "error", "INFO": "info", "TEAM_ALLY": "accent", "TEAM_ENEMY": "error",
    "OVERLAY_BACKGROUND": "overlay_background",
}
_current_theme = "dark"


def current_theme():
    return _current_theme


def palette_for(theme=None):
    return PALETTES.get(theme or _current_theme, PALETTES["dark"])


def set_color_theme(theme):
    global _current_theme
    if theme not in PALETTES:
        raise ValueError("Unknown theme")
    _current_theme = theme
    palette = PALETTES[theme]
    for alias, role in COLOR_ROLES.items():
        setattr(Colors, alias, getattr(palette, role))


class Colors:
    """Compatibility aliases, populated exclusively from the semantic palette."""
    ON_ACCENT = "#ffffff"
    MEDIA_BACKGROUND = "#000000"  # Media pixels keep their own colors.


set_color_theme("dark")
