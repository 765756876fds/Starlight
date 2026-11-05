"""Application version; VERSION is shared by source, frozen builds and installers."""
from pathlib import Path
import re
import sys


def get_version(root=None):
    root = Path(root) if root is not None else Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    try:
        value = (root / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "unknown"
    return value if re.fullmatch(r"\d+\.\d+\.\d+(?:\.dev\d+)?", value) else "unknown"


__version__ = get_version()
