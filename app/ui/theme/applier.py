from pathlib import Path
from PySide6.QtWidgets import QApplication


def apply_dark_theme(app: QApplication) -> None:
    qss_path = Path(__file__).parent / "dark.qss"
    with open(qss_path, "r", encoding="utf-8") as f:
        app.setStyleSheet(f.read())
