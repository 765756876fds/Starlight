"""QSystemTrayIcon 版托盘（星芒）。"""
from pathlib import Path

from PySide6.QtGui import QAction, QGuiApplication, QIcon, QPixmap, QPainter, QColor, QBrush
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from app.ui.theme.colors import Colors


def _make_icon() -> QIcon:
    # 优先使用自定义图标（源码态 = 项目根 assets/icon/app.ico；
    # 打包态 spec 收集到 _internal/assets/icon/app.ico，__file__ 相对自动命中）
    icon_path = Path(__file__).resolve().parents[2] / "assets" / "icon" / "app.ico"
    if icon_path.is_file():
        return QIcon(str(icon_path))
    # 兜底：自绘色块（图标文件缺失时）
    pix = QPixmap(64, 64)
    pix.fill(QColor(Colors.ACCENT))
    p = QPainter(pix)
    p.setBrush(QBrush(QColor("#ffffff")))
    p.setPen(QColor("#ffffff"))
    p.drawEllipse(12, 12, 40, 40)
    p.setBrush(QBrush(QColor(Colors.ACCENT)))
    p.drawEllipse(24, 24, 16, 16)
    p.end()
    return QIcon(pix)


class TrayIcon:
    def __init__(self, window, on_quit):
        self._window = window
        self._on_quit = on_quit
        self._icon = QSystemTrayIcon(window)
        self._icon.setIcon(_make_icon())
        self._icon.setToolTip("星芒")

        menu = QMenu()
        show_act = QAction("显示主窗口", window)
        show_act.triggered.connect(self._show_window)
        menu.addAction(show_act)
        menu.addSeparator()
        quit_act = QAction("退出", window)
        quit_act.triggered.connect(self._quit)
        menu.addAction(quit_act)
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._on_activated)

    def show(self):
        self._icon.show()

    def _show_window(self):
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_window()

    def _quit(self):
        try:
            self._on_quit()
        except Exception:
            pass
        QApplication.quit()
