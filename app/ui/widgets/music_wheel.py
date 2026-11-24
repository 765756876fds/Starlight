"""Consistent vertical scrolling for the music views and nested discovery cards."""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractItemView, QAbstractScrollArea, QApplication


class MusicWheelScroll(QObject):
    def install(self, area):
        if isinstance(area, QAbstractItemView):
            area.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        area.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Wheel or event.modifiers() & (
                Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier):
            return False
        pixel = event.pixelDelta()
        delta = pixel if not pixel.isNull() else event.angleDelta()
        if not delta.y() or abs(delta.x()) > abs(delta.y()):
            return False
        distance = delta.y()
        if pixel.isNull():
            lines = QApplication.wheelScrollLines()
            distance = round(distance * lines * 40 / 120)
        if not distance:
            return False

        area = watched.parentWidget()
        while area is not None:
            if (isinstance(area, QAbstractScrollArea)
                    and area.verticalScrollBarPolicy() != Qt.ScrollBarAlwaysOff):
                bar = area.verticalScrollBar()
                previous = bar.value()
                bar.setValue(previous - distance)
                distance -= previous - bar.value()
                if not distance:
                    break
            area = area.parentWidget()
        event.accept()
        return True
