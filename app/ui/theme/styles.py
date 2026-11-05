"""Semantic bindings for existing inline QSS; never invert arbitrary colors.

Only the application's palette values are bound. White-on-accent, artwork,
video pixels and user-selected colors are not part of this compatibility map.
"""
import re

from .colors import PALETTES

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\b|rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+(?:\s*,\s*[\d.]+)?\s*\)")
_TOKEN = re.compile(r"@theme\((\w+)(,rgb)?\)")


def style_template(text, theme):
    palette = PALETTES[theme]
    mapping = {}
    # First prefer the current palette, then recognize legacy dark literals.
    for colors in (palette, PALETTES["dark"]):
        for role, value in vars(colors).items():
            mapping.setdefault(value.lower(), role)
            rgb = tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
            mapping.setdefault(rgb, role)

    def replace(match):
        value = match.group()
        if value.startswith("#"):
            role = mapping.get(value.lower())
            return f"@theme({role})" if role else value
        numbers = re.findall(r"[\d.]+", value)
        role = mapping.get(tuple(int(n) for n in numbers[:3]))
        if not role:
            return value
        alpha = ", " + numbers[3] if len(numbers) == 4 else ""
        return f"{'rgba' if alpha else 'rgb'}(@theme({role},rgb){alpha})"

    return _COLOR.sub(replace, text)


def render_style(template, theme):
    palette = PALETTES[theme]

    def replace(match):
        value = getattr(palette, match.group(1))
        return ", ".join(str(int(value[i:i + 2], 16)) for i in (1, 3, 5)) if match.group(2) else value

    return _TOKEN.sub(replace, template)
