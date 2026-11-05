"""One GUI-thread theme owner; recolor existing widgets without recreating them."""
import logging
import weakref

from PySide6.QtCore import QObject, QEvent, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPalette, QPixmapCache
from PySide6.QtWidgets import QApplication, QLabel, QWidget, QAbstractItemView
from shiboken6 import isValid

from .colors import Colors, PALETTES, current_theme, palette_for, set_color_theme
from .styles import style_template, render_style
from .tokens import refresh_color_tokens

logger = logging.getLogger(__name__)


class ThemeManager(QObject):
    theme_changed = Signal(str)
    _requested = Signal(str)

    def __init__(self, app):
        if QThread.currentThread() != app.thread():
            raise RuntimeError("ThemeManager must be created on the GUI thread")
        super().__init__(app)
        self.app = app
        self.mode = current_theme()
        self.last_error = ""
        self._updating = False
        self._styles = weakref.WeakKeyDictionary()
        self._pending = weakref.WeakSet()
        self._scheduled = False
        self._requested.connect(self.set_theme, Qt.QueuedConnection)
        app.installEventFilter(self)
        app.setStyle("Fusion")
        from ..settings_store import load_ui_prefs
        self.set_theme(load_ui_prefs().get("theme", "dark"), persist=False, force=True)

    def request_theme(self, mode):
        """Background callers request a GUI-thread change, without touching widgets."""
        if mode not in PALETTES:
            raise ValueError("Unknown theme")
        self._requested.emit(mode)

    @Slot(str)
    def set_theme(self, mode, *, persist=True, force=False):
        if QThread.currentThread() != self.app.thread():
            raise RuntimeError("Use request_theme outside the GUI thread")
        if mode not in PALETTES:
            raise ValueError("Unknown theme")
        self.last_error = ""
        if persist:
            from ..settings_store import save_ui_prefs
            if not save_ui_prefs({"theme": mode}):
                self.last_error = "主题已切换，但偏好未保存；下次启动可能恢复原主题。"
                logger.warning("theme_preference_save_failed")
        if force or mode != self.mode:
            previous = self.mode
            widgets = list(self.app.allWidgets())
            for widget in widgets:
                self._capture_style(widget, previous)
            self._updating = True
            try:
                self.mode = mode
                set_color_theme(mode)
                refresh_color_tokens()
                self._apply_palette()
                from .applier import build_glass_qss
                self.app.setStyleSheet(build_glass_qss())
                QPixmapCache.clear()
                for widget in widgets:
                    if not isValid(widget):
                        continue
                    self._apply_widget(widget)
                    if isinstance(widget, QLabel) and widget.textFormat() != Qt.PlainText:
                        text = widget.text()
                        if "<" in text and ">" in text:
                            widget.setText(render_style(style_template(text, previous), mode))
            finally:
                self._updating = False
            self.theme_changed.emit(mode)
        if self.last_error:
            from ...core.notifications import notify
            notify("主题偏好未保存", self.last_error, status="warning", source="settings")
        return not self.last_error

    def toggle(self):
        return self.set_theme("light" if self.mode == "dark" else "dark")

    def _apply_palette(self):
        colors = palette_for(self.mode)
        palette = QPalette()
        roles = {
            QPalette.Window: colors.panel_background, QPalette.WindowText: colors.primary_text,
            QPalette.Base: colors.elevated_background, QPalette.AlternateBase: colors.hover_background,
            QPalette.Text: colors.primary_text, QPalette.Button: colors.elevated_background,
            QPalette.ButtonText: colors.primary_text, QPalette.ToolTipBase: colors.elevated_background,
            QPalette.ToolTipText: colors.primary_text, QPalette.Highlight: colors.accent,
            QPalette.HighlightedText: Colors.ON_ACCENT, QPalette.Link: colors.accent,
            QPalette.PlaceholderText: colors.secondary_text,
        }
        for role, color in roles.items():
            palette.setColor(role, QColor(color))
        for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
            palette.setColor(QPalette.Disabled, role, QColor(colors.disabled_text))
        self.app.setPalette(palette)

    def _capture_style(self, widget, source_theme):
        if widget.property("themeIndependent"):
            return
        sheet = widget.styleSheet()
        old = self._styles.get(widget)
        if sheet and (old is None or old[1] != sheet):
            self._styles[widget] = (style_template(sheet, source_theme), sheet)
        elif not sheet:
            self._styles.pop(widget, None)

    def _apply_widget(self, widget):
        if not isValid(widget):
            return
        try:
            entry = self._styles.get(widget)
            if entry and not widget.property("themeIndependent"):
                rendered = render_style(entry[0], self.mode)
                if rendered != widget.styleSheet():
                    widget.setStyleSheet(rendered)
                self._styles[widget] = (entry[0], rendered)
            if isinstance(widget, QAbstractItemView):
                widget.viewport().update()
            from ..widgets.icon_utils import refresh_widget_icons
            refresh_widget_icons(widget)
            QWidget.update(widget)  # Item views overload update(QModelIndex).
        except RuntimeError:
            # A closed window may have been deleted between queued updates.
            self._styles.pop(widget, None)

    def eventFilter(self, watched, event):
        if not self._updating and isinstance(watched, QWidget) and event.type() in (
                QEvent.Polish, QEvent.Show):
            # Never observe StyleChange globally: Qt can emit it while replacing
            # or destroying its internal stylesheet objects. Re-entering that
            # path from a Python event filter can crash native widget cleanup.
            # Runtime status producers use current Tokens; legacy inline styles
            # are bound when shown and during an explicit theme change.
            self._capture_style(watched, self.mode)
            self._pending.add(watched)
            if not self._scheduled:
                self._scheduled = True
                QTimer.singleShot(0, self._flush_pending)
        return False

    def _flush_pending(self):
        self._scheduled = False
        widgets = list(self._pending)
        self._pending.clear()
        self._updating = True
        try:
            for widget in widgets:
                self._apply_widget(widget)
        finally:
            self._updating = False


def get_theme_manager(app=None):
    app = app or QApplication.instance()
    if app is None:
        raise RuntimeError("A QApplication is required")
    manager = getattr(app, "_starlight_theme_manager", None)
    if manager is None:
        manager = ThemeManager(app)
        app._starlight_theme_manager = manager
    return manager
