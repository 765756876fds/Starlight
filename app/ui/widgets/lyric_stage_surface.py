"""Full-resolution texture compositing for the Windows raster-backed lyric stage."""
import logging

from PySide6.QtCore import QObject, Qt, QTimer, Slot
from PySide6.QtGui import QGuiApplication, QImage, QOffscreenSurface, QOpenGLContext, QPainter
from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLPaintDevice
from shiboken6 import isValid

from .lyric_stage_glow import GlyphGlow

logger = logging.getLogger(__name__)


class StageSurface(QObject):
    """Draw with the existing painter and hand one native-size frame to the layered window."""

    def __init__(self, parent):
        super().__init__(parent)
        self.retire = QTimer(self)
        self.retire.setSingleShot(True)
        self.retire.setInterval(3000)
        self.retire.timeout.connect(self.close)
        parent.destroyed.connect(self.close)
        QGuiApplication.instance().aboutToQuit.connect(self.close)
        self.context = None
        self.surface = None
        self.target = None
        self.device = None
        self.unavailable = False
        self.glow = None
        self._retire_glow = False
        self._glow_failed = False

    @staticmethod
    def available():
        return (QGuiApplication.platformName() == "windows"
                and not QGuiApplication.testAttribute(Qt.AA_UseSoftwareOpenGL))

    @staticmethod
    def warm_driver():
        # Qt initializes its Windows graphics integration here. Do this on the GUI
        # thread while the music page is built, before the first lyric transition.
        QOpenGLContext.supportsThreadedOpenGL()

    def _open(self, screen):
        self.context = QOpenGLContext()
        self.context.setScreen(screen)
        if not self.context.create():
            return False
        self.surface = QOffscreenSurface(screen)
        self.surface.setFormat(self.context.format())
        self.surface.create()
        if not self.surface.isValid() or not self.context.makeCurrent(self.surface):
            return False
        name = self.context.functions().glGetString(0x1F01)  # GL_RENDERER
        software = ("llvmpipe", "softpipe", "gdi generic", "software", "swiftshader", "basic render")
        return bool(name) and not any(kind in str(name).lower() for kind in software)

    def frame(self, widget, paint):
        if self.unavailable:
            return QImage()
        self.retire.stop()
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous else None
        try:
            ready = (self._open(widget.screen()) if self.context is None
                     else self.context.makeCurrent(self.surface))
            if not ready:
                self.unavailable = True
                logger.info("Lyric stage using raster painter (graphics context unavailable)")
                return QImage()
            if self.glow is not None and self._retire_glow:
                self.glow.release()
                self.glow = None
            self._retire_glow = False
            ratio = widget.devicePixelRatioF()
            size = widget.size() * ratio
            if self.target is None or self.target.size() != size:
                self.device = None
                self.target = QOpenGLFramebufferObject(size, QOpenGLFramebufferObject.CombinedDepthStencil)
                if self.target.isValid():
                    self.device = QOpenGLPaintDevice(size)
            if self.device is None or not self.target.bind():
                return QImage()
            self.device.setDevicePixelRatio(ratio)
            with QPainter(self.device) as painter:
                if not painter.isActive():
                    self.unavailable = True
                    return QImage()
                paint(painter)
            # Qt’s standard readback preserves projective glyph transforms at every DPR.
            image = self.target.toImage()
            self.target.release()
            if image.isNull():
                self.unavailable = True
                return image
            image.setDevicePixelRatio(ratio)
            return image
        finally:
            if self.context is not None and QOpenGLContext.currentContext() is self.context:
                self.context.doneCurrent()
            if previous is not None and previous_surface is not None:
                previous.makeCurrent(previous_surface)
            if self.unavailable:
                self.close()

    def paint_glyph_effect(self, painter, *args):
        if self._glow_failed or self.device is None or painter.device() is not self.device:
            return False
        if self.glow is None:
            self.glow = GlyphGlow()
        try:
            return self.glow.paint(painter, self.context, self.target.size(), *args)
        except (RuntimeError, TypeError, ValueError):
            logger.exception("Lyric effects shader failed; using cached Qt materials")
            self.glow.release()
            self.glow = None
            self._glow_failed = True
            return False

    def retire_effects(self):
        self._retire_glow = True

    def suspend(self):
        if self.context is not None and not self.retire.isActive():
            self.retire.start()

    @Slot()
    def close(self):
        self.retire.stop()
        if self.glow is not None:
            previous = QOpenGLContext.currentContext()
            surface = previous.surface() if previous is not None else None
            if (self.context is not None and isValid(self.context) and self.surface is not None
                    and isValid(self.surface)
                    and self.context.makeCurrent(self.surface)):
                self.glow.release()
                self.context.doneCurrent()
                if previous is not None and previous is not self.context and surface is not None:
                    previous.makeCurrent(surface)
            self.glow = None
        # Qt frees shared texture caches with this context. No window/swap chain or
        # render thread outlives the stage, including after a failed initialization.
        self.device = self.target = None
        self.context = None
        # QOffscreenSurface destroys its native window in its destructor. Qt may
        # already have destroyed it during application shutdown.
        self.surface = None
