"""Cross-thread delivery to Qt, with explicit subscription cleanup."""
import weakref
from PySide6.QtCore import QObject, Signal


class NotificationStream(QObject):
    changed = Signal()

    def __init__(self, center, parent=None):
        super().__init__(parent)
        ref = weakref.ref(self)

        def deliver():
            owner = ref()
            if owner is not None:
                try:
                    owner.changed.emit()
                except RuntimeError:
                    pass

        self._unsubscribe = center.subscribe(deliver)
        self.destroyed.connect(lambda: center.unsubscribe(deliver))

    def dispose(self):
        self._unsubscribe()
