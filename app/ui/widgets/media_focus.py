"""GUI-only media focus hints. Live playback is still checked by the worker."""
from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import QApplication


class MediaFocusTracker(QObject):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.FocusIn, QEvent.MouseButtonPress, QEvent.WindowActivate):
            widget = obj
            domain = None
            while widget is not None:
                domain = widget.property("starlightMediaDomain")
                if domain in ("music", "movie"):
                    break
                widget = widget.parent()
            self.context.note_focus(domain)
        return False
