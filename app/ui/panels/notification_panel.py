"""User-facing notification history. Raw logs never enter this view."""
from datetime import datetime
from html import escape

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt, Slot, QRect
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListView, QPushButton,
                               QStyledItemDelegate, QVBoxLayout, QWidget, QPlainTextEdit)

from ...core.notifications import get_notification_center
from ..theme.notification_style import notification_style, STATUS_LABELS, SPEECH_NOTES, status_icon
from ..widgets.notification_stream import NotificationStream
from ..widgets.icon_utils import lucide_icon


def notification_text(item):
    parts = [datetime.fromtimestamp(item.created_at).strftime("%H:%M:%S"),
             STATUS_LABELS.get(item.status, "提醒"), item.title]
    if item.heard:
        parts.append("你说：" + item.heard)
    parts.extend(value for value in (item.message, item.advice, SPEECH_NOTES.get(item.speech_status)) if value)
    return "\n".join(parts)


class NotificationListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = ()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role in (Qt.DisplayRole, Qt.AccessibleTextRole):
            return notification_text(item)
        if role == Qt.UserRole:
            return item
        if role == Qt.ToolTipRole:
            return "<qt>" + escape(notification_text(item)).replace("\n", "<br>") + "</qt>"

    def set_items(self, items):
        if items != self.items:
            self.beginResetModel()
            self.items = items
            self.endResetModel()


class NotificationDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, theme=None):
        super().__init__(parent)
        self._theme_override = theme

    @property
    def style(self):
        return notification_style(self._theme_override)

    def sizeHint(self, option, index):
        item = index.data(Qt.UserRole)
        return QSize(240, 160 if item and item.speech_status in SPEECH_NOTES else 144)

    def paint(self, painter, option, index):
        item = index.data(Qt.UserRole)
        if item is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(3, 3, -3, -3)
        painter.setClipRect(rect)
        painter.setPen(QColor(self.style.border))
        painter.setBrush(QColor(self.style.surface))
        painter.drawRoundedRect(rect, 12, 12)
        color = self.style.color(item.status)
        lucide_icon(status_icon(item.status), 14, color).paint(painter, rect.x() + 14, rect.y() + 9, 14, 14)
        painter.setPen(QColor(color))
        painter.drawText(rect.adjusted(36, 8, -14, -8), Qt.AlignTop | Qt.AlignLeft,
                         STATUS_LABELS.get(item.status, "提醒"))
        painter.setPen(QColor(self.style.secondary))
        painter.drawText(rect.adjusted(14, 8, -14, -8), Qt.AlignTop | Qt.AlignRight,
                         datetime.fromtimestamp(item.created_at).strftime("%H:%M:%S"))
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(self.style.text))
        metrics = painter.fontMetrics()
        painter.drawText(rect.adjusted(14, 32, -14, -8), Qt.AlignTop,
                         metrics.elidedText(item.title, Qt.ElideRight, max(10, rect.width() - 28)))
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(self.style.secondary))
        body = "\n".join(part for part in (
            "你说：" + item.heard if item.heard else "", item.message, item.advice) if part)
        painter.drawText(QRect(rect.x() + 14, rect.y() + 58, max(10, rect.width() - 28), 54),
                         Qt.AlignTop | Qt.TextWordWrap, body)
        note = SPEECH_NOTES.get(item.speech_status)
        if note:
            painter.setPen(QColor(self.style.warning))
            painter.drawText(rect.adjusted(14, 118, -14, -8), Qt.AlignTop,
                             painter.fontMetrics().elidedText(note, Qt.ElideRight, max(10, rect.width() - 28)))
        painter.restore()


class NotificationPanel(QWidget):
    def __init__(self, parent=None, center=None):
        super().__init__(parent)
        self.center = center or get_notification_center()
        colors = notification_style()
        self.setObjectName("notificationPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"QWidget#notificationPanel {{ background: {colors.background}; }}"
                           f"QListView {{ background: {colors.background}; border: none; }}"
                           f"QLabel {{ color: {colors.text}; }}"
                           f"QPlainTextEdit {{ background: {colors.surface}; color: {colors.text};"
                           f"border: 1px solid {colors.border}; border-radius: 10px; padding: 8px; }}")
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("通知中心")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch()
        self.btn_clear = QPushButton("清空通知")
        self.btn_clear.clicked.connect(self.center.clear)
        header.addWidget(self.btn_clear)
        root.addLayout(header)
        self.summary = QLabel("最近的操作和提醒会留在这里，便于查看结果。")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.model = NotificationListModel(self)
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(NotificationDelegate(self.view))
        self.view.setUniformItemSizes(False)
        self.view.setSpacing(3)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setWordWrap(True)
        root.addWidget(self.view, 1)
        self.empty = QLabel("还没有通知。播放音乐、识曲或使用语音后，可以在这里查看结果。")
        self.empty.setWordWrap(True)
        root.addWidget(self.empty)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(150)
        self.details.setPlaceholderText("选择一条通知查看完整说明")
        root.addWidget(self.details)
        self.view.selectionModel().currentChanged.connect(self._select)
        self.stream = NotificationStream(self.center, self)
        self.stream.changed.connect(self.refresh, Qt.QueuedConnection)
        self.refresh()

    @Slot()
    def refresh(self):
        selected = self.view.currentIndex().data(Qt.UserRole)
        items = self.center.history()
        scroll = self.view.verticalScrollBar().value()
        self.model.set_items(items)
        self.empty.setVisible(not items)
        self.summary.setText(f"最近 {len(items)} 条 · 最多保留 {self.center.limit} 条，关闭星芒后不保留")
        self.btn_clear.setEnabled(bool(items))
        if selected:
            index = next((i for i, row in enumerate(items) if row.id == selected.id), None)
            if index is not None:
                self.view.setCurrentIndex(self.model.index(index))
                self.details.setPlainText(notification_text(items[index]))
        elif items:
            self.view.setCurrentIndex(self.model.index(0))
        if not items:
            self.details.clear()
        self.view.verticalScrollBar().setValue(scroll)

    def _select(self, current, previous):
        item = current.data(Qt.UserRole)
        self.details.setPlainText(notification_text(item) if item else "")
