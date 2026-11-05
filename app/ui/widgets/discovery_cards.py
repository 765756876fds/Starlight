"""Small preview rails and virtualized category grids; no synchronous image IO."""
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainterPath, QPainter
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QListWidget, QListWidgetItem, QStyledItemDelegate, QListView, QStyle)
from urllib.parse import urlsplit

from ..theme.tokens import Tokens
from .music_card_delegate import _get_cover_loader

DISPLAY_ROLE = Qt.UserRole + 81


def label(text="", secondary=False):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.PlainText)
    if secondary:
        widget.setProperty("class", "secondary")
    return widget


class ContentDelegate(QStyledItemDelegate):
    def __init__(self, view, portrait):
        super().__init__(view)
        self.portrait = portrait
        self.loader = _get_cover_loader()
        self.loader.register_view(view)
        self.loader.loaded.connect(self._loaded)

    def _loaded(self, key):
        self.parent().viewport().update()

    def paint(self, painter, option, index):
        data = index.data(DISPLAY_ROLE) or {}
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            rect = QRectF(option.rect).adjusted(6, 6, -6, -6)
            cover = QRectF(rect.x(), rect.y(), rect.width(), rect.width() * (.92 if self.portrait else .42))
            path = QPainterPath()
            path.addRoundedRect(cover, 8, 8)
            painter.fillPath(path, QColor(Tokens.BG_HOVER))
            url = data.get("cover_url")
            key = "net:" + url if isinstance(url, str) and urlsplit(url).scheme in ("https", "http") else None
            if key:
                self.loader.request(key)  # Existing QNetworkAccessManager, asynchronous.
            pixmap = self.loader.get(key) if key else None
            if pixmap is not None and not pixmap.isNull():
                painter.save()
                painter.setClipPath(path)
                painter.setRenderHint(QPainter.SmoothPixmapTransform)
                ratio = cover.width() / cover.height()
                width, height = pixmap.width(), pixmap.height()
                if width / height > ratio:
                    crop_width = height * ratio
                    source = QRectF((width - crop_width) / 2, 0, crop_width, height)
                else:
                    crop_height = width / ratio
                    source = QRectF(0, (height - crop_height) / 2, width, crop_height)
                painter.drawPixmap(cover, pixmap, source)
                painter.restore()
            else:
                font = QFont(option.font)
                if self.portrait:
                    # A real missing poster, never an unrelated stock image.
                    ink = QColor(Tokens.TEXT_SECONDARY)
                    ink.setAlpha(110)
                    painter.setPen(ink)
                    frame = QRectF(cover.center().x() - 16, cover.center().y() - 33, 32, 28)
                    painter.drawRoundedRect(frame, 4, 4)
                    for x in (frame.left() + 3, frame.right() - 6):
                        for y in (frame.top() + 4, frame.top() + 12, frame.top() + 20):
                            painter.fillRect(QRectF(x, y, 3, 3), ink)
                    font.setPixelSize(20)
                    painter.setFont(font)
                    painter.setPen(QColor(Tokens.TEXT_SECONDARY))
                    title = str(data.get("title") or "星芒")
                    painter.drawText(QRectF(cover.x() + 8, cover.center().y(), cover.width() - 16, 26),
                                     Qt.AlignCenter, QFontMetrics(font).elidedText(title, Qt.ElideRight, int(cover.width() - 16)))
                    font.setPixelSize(Tokens.FONT_SIZE_SUB)
                    painter.setFont(font)
                    hint = "海报暂不可用" if key and self.loader.failed(key) else "正在加载海报" if key else "暂无海报"
                    painter.drawText(QRectF(cover.x(), cover.center().y() + 27, cover.width(), 18), Qt.AlignCenter, hint)
                else:
                    font.setPixelSize(28)
                    painter.setFont(font)
                    painter.setPen(QColor(Tokens.TEXT_SECONDARY))
                    painter.drawText(cover, Qt.AlignCenter, str(data.get("title") or "星芒")[:1])
            font = QFont(option.font)
            font.setPixelSize(Tokens.FONT_SIZE_BODY)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(Tokens.ACCENT if option.state & QStyle.State_Selected else Tokens.TEXT_PRIMARY))
            painter.drawText(QRectF(rect.x(), cover.bottom() + 8, rect.width(), 22), Qt.AlignVCenter,
                             QFontMetrics(font).elidedText(str(data.get("title") or "未命名"), Qt.ElideRight, int(rect.width())))
            font.setBold(False)
            font.setPixelSize(Tokens.FONT_SIZE_SUB)
            painter.setFont(font)
            painter.setPen(QColor(Tokens.TEXT_SECONDARY))
            for offset, key_name in ((32, "subtitle"), (51, "caption")):
                if key_name == "caption" and not data.get("subtitle"):
                    offset = 32
                painter.drawText(QRectF(rect.x(), cover.bottom() + offset, rect.width(), 18), Qt.AlignVCenter,
                                 QFontMetrics(font).elidedText(str(data.get(key_name) or ""), Qt.ElideRight, int(rect.width())))
        finally:
            painter.restore()


class CardList(QListWidget):
    def __init__(self, parent=None, *, portrait=False, preview=False):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(not preview)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setLayoutMode(QListView.Batched)
        self.setEditTriggers(QListView.NoEditTriggers)
        self.setGridSize(QSize(150, 220 if portrait else 144))
        self.setItemDelegate(ContentDelegate(self, portrait))
        self.setStyleSheet("QListWidget { background: transparent; border: none; }")
        if preview:
            self.setFixedHeight(self.gridSize().height() + 16)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded if preview else Qt.ScrollBarAlwaysOff)

    def set_rows(self, rows, describe):
        self.clear()
        for row in rows:
            display = describe(row)
            item = QListWidgetItem(str(display.get("title") or "未命名"))
            item.setSizeHint(self.gridSize())
            item.setData(Qt.UserRole, row)
            item.setData(DISPLAY_ROLE, display)
            item.setToolTip("\n".join(str(display.get(k) or "") for k in ("title", "subtitle", "caption")))
            if isinstance(row, dict) and row.get("available") is False:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.addItem(item)


class ContentSection(QWidget):
    def __init__(self, title, parent=None, *, portrait=False, action="查看全部"):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        head = QHBoxLayout()
        self.title = label(title)
        self.title.setProperty("class", "title")
        self.all_button = QPushButton(action)
        head.addWidget(self.title, 1)
        head.addWidget(self.all_button)
        layout.addLayout(head)
        self.hint = label("", True)
        layout.addWidget(self.hint)
        self.cards = CardList(self, portrait=portrait, preview=True)
        layout.addWidget(self.cards)
        self.set_rows([], lambda row: row, "暂无内容")

    def set_rows(self, rows, describe, empty="暂无内容"):
        self.cards.set_rows(rows, describe)
        self.cards.setVisible(bool(rows))
        self.hint.setText("" if rows else empty)
        self.hint.setVisible(not rows)
