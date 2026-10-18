from pathlib import Path
from PySide6.QtWidgets import QApplication


def _load_qss(name: str) -> str:
    qss_path = Path(__file__).parent / name
    with open(qss_path, "r", encoding="utf-8") as f:
        return f.read()


def apply_dark_theme(app: QApplication) -> None:
    app.setStyleSheet(_load_qss("dark.qss"))


def apply_glass_theme(app: QApplication) -> None:
    """Glass 全局样式（覆盖 dark 主题；MainWindow.__init__ 调用）。"""
    app.setStyleSheet(_load_qss("theme.qss"))
