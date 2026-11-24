"""Artwork and compact shared controls for music browsing and playback."""
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..theme.tokens import Tokens
from .icon_utils import lucide_icon
from .music_card_delegate import _get_cover_loader, cover_key_of


class Artwork(QWidget):
    def __init__(self, parent=None, *, radius=8):
        super().__init__(parent)
        self._radius = radius
        self.key = None
        self.loader = _get_cover_loader()
        self.loader.loaded.connect(self._loaded)

    def set_song(self, song):
        key = cover_key_of(song)
        if key != self.key:
            self.key = key
            if key:
                self.loader.request(key)
            self.update()

    def _loaded(self, key):
        if key == self.key:
            self.update()

    def paintEvent(self, event):
        rect = QRectF(self.rect())
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(rect, self._radius, self._radius)
        painter.setClipPath(clip)
        painter.fillRect(rect, QColor(Tokens.BG_ELEVATED))
        image = self.loader.get(self.key) if self.key else None
        if image and not image.isNull():
            scale = max(rect.width()/image.width(), rect.height()/image.height())
            wide, high = rect.width()/scale, rect.height()/scale
            source = QRectF((image.width()-wide)/2, (image.height()-high)/2, wide, high)
            painter.drawPixmap(rect, image, source)
        else:
            size = min(28, self.height()//2)
            icon = lucide_icon("music", size, Tokens.TEXT_SECONDARY)
            icon.paint(painter, self.rect(), Qt.AlignCenter)


class PlaylistHeader(Artwork):
    """Compact playlist header with a cover crop kept away from the title."""

    def __init__(self, parent=None):
        super().__init__(parent, radius=10)
        self.setFixedHeight(78)

    def paintEvent(self, event):
        rect = QRectF(self.rect())
        artwork = self.loader.get(self.key) if self.key else None
        base = QColor(Tokens.BG_ELEVATED)
        with QPainter(self) as painter:
            painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
            clip = QPainterPath()
            clip.addRoundedRect(rect, self._radius, self._radius)
            painter.setClipPath(clip)
            painter.fillRect(rect, base)
            if artwork and not artwork.isNull():
                width = min(rect.width() * .48, 245)
                crop = QRectF(rect.right() - width, 0, width, rect.height())
                h = min(artwork.height(), artwork.width() * crop.height() / width)
                painter.drawPixmap(crop, artwork, QRectF(0, (artwork.height() - h) / 2, artwork.width(), h))
                fade = QLinearGradient(crop.left(), 0, crop.right(), 0)
                fade.setColorAt(0, base)
                veil = QColor(base)
                veil.setAlpha(186)
                fade.setColorAt(1, veil)
                painter.fillRect(crop, fade)


class FeatureSheet(QFrame):
    activated = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("featureSheet")
        self.setStyleSheet(f"QFrame#featureSheet {{ background: {Tokens.BG_ELEVATED}; border-radius: 12px; }}")
        self.setMinimumHeight(132)
        self.setMaximumHeight(156)
        self.row = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(18, 10, 10, 10)
        copy = QVBoxLayout()
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.heading.setTextFormat(Qt.PlainText)
        font = self.heading.font()
        font.setPointSize(18)
        font.setWeight(font.Weight.DemiBold)
        self.heading.setFont(font)
        self.heading.setStyleSheet("font-size:28px; font-weight:600;")
        self.creator = QLabel()
        self.creator.setTextFormat(Qt.PlainText)
        self.open = QPushButton("查看歌单")
        self.open.clicked.connect(lambda: self.activated.emit(self.row))
        copy.addStretch()
        copy.addWidget(self.heading)
        copy.addWidget(self.creator)
        copy.addWidget(self.open, 0, Qt.AlignLeft)
        copy.addStretch()
        self.art = Artwork()
        self.art.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addLayout(copy, 2)
        outer.addWidget(self.art, 3)
        self.hide()

    def set_sheet(self, row):
        self.row = row
        self.setVisible(bool(row))
        if not row:
            self.art.set_song({})
            return
        self.heading.setText(str(row.get("title") or row.get("name") or "歌单"))
        author = row.get("artist") or row.get("creator") or ""
        if isinstance(author, dict):
            author = author.get("name") or ""
        self.creator.setText(str(author))
        self.creator.setVisible(bool(author))
        self.art.set_song({"cover_url": row.get("artwork") or row.get("cover_img") or row.get("cover_url")})


class TrackColumns(QWidget):
    def __init__(self, listing, sort, download=False):
        super().__init__()
        self.download = download
        self.listing = listing
        self.labels = []
        for key, title in (("title","歌曲"),("artist","歌手"),("album","专辑"),("duration","时长")):
            button = QPushButton(title, self)
            button.setFlat(True)
            button.setAccessibleName("按" + title + "排序")
            button.clicked.connect(lambda checked=False, field=key: sort(field))
            button.setStyleSheet(f"QPushButton {{font-size:12px; text-align:left; padding:0; border:0; color:{Tokens.TEXT_SECONDARY};}}")
            self.labels.append(button)
        self.setFixedHeight(24)
        self.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        available = max(1, self.width() - (60 if self.download else 0) - 55)
        show_artist, show_album = available >= 380, available >= 510
        duration = 48 if available >= 300 else 0
        work = available - duration
        title = int(work * (.45 if show_album else .61)) if show_artist else work
        artist = int(work * .25) if show_album else work - title
        x = 50
        for button, width in zip(self.labels, (title,artist,work-title-artist,duration)):
            button.setVisible(width > 0)
            button.setGeometry(x, 0, max(1,width-5), self.height())
            x += width
