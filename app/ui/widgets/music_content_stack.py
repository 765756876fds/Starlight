"""Input-transparent transitions over live music and movie navigation."""
import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QStackedWidget, QTabWidget, QWidget

from ..theme.motion import DUR_CARD, DUR_HOVER, EASE_IN_OUT, EASE_OUT, reduced_motion_enabled
from ..theme.tokens import Tokens


def _snapshot(page, limit):
    """Bound capture cost even on high-DPI displays; geometry stays in logical pixels."""
    if page.width() <= 0 or page.height() <= 0:
        return QPixmap()
    scale = min(page.devicePixelRatioF(), math.sqrt(limit / (page.width() * page.height())))
    frame = QPixmap(max(1, round(page.width() * scale)), max(1, round(page.height() * scale)))
    frame.fill(QColor(Tokens.BG_BASE))
    with QPainter(frame) as painter:
        painter.scale(scale, scale)
        page.render(painter, QPoint())
    return frame


class PageTransition(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.frame = QPixmap()
        self.art = QPixmap()
        self.origin = QRectF()
        self.destination = QRectF()
        self.direction = 1
        self.immersive = False
        self.progress = 1.
        self.animation = QVariantAnimation(self)
        self.animation.setStartValue(0.)
        self.animation.setEndValue(1.)
        self.animation.valueChanged.connect(self._advance)
        self.animation.finished.connect(self.finish)
        self.hide()

    def start(self, frame, geometry, direction, *, immersive=False, shared=None):
        self.finish()
        if frame.isNull():
            return
        self.setGeometry(geometry)
        self.frame = frame
        self.direction = direction
        self.immersive = immersive
        self.progress = 0.
        if shared:
            self.art, self.origin, self.destination = shared
            # Cut the moving cover from its snapshot so only one copy departs.
            if direction > 0 and not self.art.isNull():
                with QPainter(self.frame) as p:
                    p.scale(self.frame.width() / max(1, self.width()), self.frame.height() / max(1, self.height()))
                    p.fillRect(self.origin, QColor(Tokens.BG_BASE))
        self.animation.setDuration(DUR_CARD + 115 if immersive else DUR_HOVER)
        self.animation.setEasingCurve(EASE_IN_OUT if immersive else EASE_OUT)
        self.raise_()
        self.show()
        self.animation.start()

    def cover_bounds(self, widget):
        return QRect(widget.mapTo(self.parentWidget(), QPoint()), widget.size())

    def wash(self, widget, color, alpha, direction=0):
        if self.isVisible():
            self.finish()
            return
        shade = QColor(color)
        shade.setAlpha(alpha)
        frame = QPixmap(1, 1)
        frame.fill(shade)
        self.start(frame, self.cover_bounds(widget), direction)

    def depart(self, widget):
        if self.isVisible():
            self.finish()
            return
        self.start(_snapshot(widget, 180_000), self.cover_bounds(widget), -1)

    def _advance(self, value):
        self.progress = value
        self.update()

    def moving_rect(self):
        t = self.progress
        bend = math.sin(math.pi * t)
        a, b = self.origin, self.destination
        center = a.center() * (1 - t) + b.center() * t + QPointF(0., -38 * bend)
        width = (a.width() * (1 - t) + b.width() * t) * (1. + .14 * bend)
        height = (a.height() * (1 - t) + b.height() * t) * (1. + .14 * bend)
        return QRectF(center - QPointF(width / 2, height / 2), center + QPointF(width / 2, height / 2))

    def paintEvent(self, event):
        if self.frame.isNull():
            return
        t = self.progress
        with QPainter(self) as p:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.setOpacity(1. - t)
            rect = QRectF(self.rect())
            if self.immersive:
                span = self.width() * .032 * t * self.direction
                rect.adjust(-span, -span * .48, span, span * .48)
                rect.translate(0, t * 23 * self.direction)
            else:
                rect.translate(-self.direction * 32 * t, 0)
            p.drawPixmap(rect, self.frame, QRectF(self.frame.rect()))
            if not self.art.isNull():
                rect = self.moving_rect()
                # The shared cover dissolves into the background at the stage
                # end of the path, where there is no static cover tile.
                blend = min(1., (1.-t)/.42 if self.direction > 0 else t/.42)
                p.setOpacity(blend)
                p.translate(rect.center())
                p.rotate(-8. * self.direction * math.sin(math.pi * t))
                p.translate(-rect.center())
                p.drawPixmap(rect, self.art, QRectF(self.art.rect()))

    def finish(self):
        self.animation.stop()
        self.hide()
        self.frame = QPixmap()
        self.art = QPixmap()


class MusicContentStack(QStackedWidget):
    entrance_progress = Signal(float)
    MAX_FRAME_PIXELS = 850_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outgoing = PageTransition(self)
        self._fade = self._outgoing.animation
        self._entry_start = 0.
        self._track_key = ""
        self._fade.valueChanged.connect(self._on_frame)
        self._fade.finished.connect(lambda: self.entrance_progress.emit(1.))

    def _on_frame(self, progress):
        if self._outgoing.direction > 0:
            self.entrance_progress.emit(self._entry_start + (1. - self._entry_start) * progress)

    def discard_stale_frame(self, key):
        if key != self._track_key and self._outgoing.isVisible():
            self.finish_transition()

    def transition_to(self, page, *, animate, shared=None, direction=1, track_key=""):
        self.discard_stale_frame(track_key)
        self._track_key = track_key
        previous = self.currentWidget()
        if previous is page:
            if not animate:
                self.finish_transition()
            return
        frame = QPixmap()
        interrupted = None
        reversing = self._outgoing.isVisible()
        entry_start = 1. - self._outgoing.progress if reversing and direction > 0 else 0.
        if animate and self.isVisible() and previous is not None:
            if not reversing:
                frame = _snapshot(previous, self.MAX_FRAME_PIXELS)
            elif self._fade.currentTime() > 16:
                if shared and not self._outgoing.art.isNull():
                    interrupted = self._outgoing.art, self._outgoing.moving_rect()
                frame = _snapshot(self, self.MAX_FRAME_PIXELS)
        self.finish_transition()
        self.setCurrentWidget(page)
        if not frame.isNull():
            if callable(shared):
                shared = shared()
            if shared and interrupted:
                shared = (*interrupted, shared[2])
            if direction > 0:
                self._entry_start = entry_start
                self.entrance_progress.emit(entry_start)
            self._outgoing.start(frame, page.geometry(), direction, immersive=True, shared=shared)

    def finish_transition(self):
        self._outgoing.finish()
        self.entrance_progress.emit(1.)

    def resizeEvent(self, event):
        self.finish_transition()
        super().resizeEvent(event)

    def hideEvent(self, event):
        self.finish_transition()
        super().hideEvent(event)


class MediaTabs(QTabWidget):
    """Move focus and input immediately. A temporary layer explains the change."""

    def __init__(self, titles=(), parent=None, *, theme_fade=False, animate_navigation=True):
        super().__init__(parent)
        self.related_tabs = frozenset(titles)
        self._animate_navigation = animate_navigation
        self.reduce_motion = reduced_motion_enabled
        self._previous = -1
        self._outgoing = PageTransition(self)
        self.currentChanged.connect(self._switch)
        if theme_fade:
            from ..theme.colors import palette_for
            from ..theme.manager import get_theme_manager
            manager = get_theme_manager()
            self._surface_color = palette_for(manager.mode).panel_background
            manager.theme_changed.connect(self._fade_theme)

    def _fade_theme(self, mode):
        from ..theme.colors import palette_for
        previous = self._surface_color
        self._surface_color = palette_for(mode).panel_background
        if self.isVisible() and self.currentWidget() is not None and not self.reduce_motion():
            self._outgoing.wash(self.currentWidget(), previous, 42)
        else:
            self._outgoing.finish()

    def _switch(self, index):
        previous, self._previous = self._previous, index
        interrupted = self._outgoing.isVisible()
        self._outgoing.finish()
        if not self._animate_navigation or interrupted or previous < 0 or index < 0 or not self.isVisible() or self.reduce_motion():
            return
        old, page = self.widget(previous), self.widget(index)
        direction = 1 if index > previous else -1
        if {self.tabText(index), self.tabText(previous)} <= self.related_tabs:
            self._outgoing.start(_snapshot(old, 360_000), self._outgoing.cover_bounds(page), direction)
        else:
            # Ordinary navigation needs no synchronous rendering of the outgoing page.
            self._outgoing.wash(page, Tokens.BG_BASE, 52, direction)

    def resizeEvent(self, event):
        self._outgoing.finish()
        super().resizeEvent(event)

    def hideEvent(self, event):
        self._outgoing.finish()
        super().hideEvent(event)
