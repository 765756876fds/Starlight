"""A single, quiet current-interaction capsule. History lives in the center."""
from html import escape
from PySide6.QtCore import Property, QPropertyAnimation, QEasingCurve, Qt, QTimer, Signal, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QPushButton, QSizePolicy
from ..theme.notification_style import notification_style, SPEECH_NOTES, status_icon
from .icon_utils import lucide_icon


class VoiceIsland(QPushButton):
    open_notifications = Signal()
    COLLAPSE_MS = 5000

    def __init__(self, parent=None, theme=None):
        super().__init__(parent)
        self._theme_override = theme
        self._record_key = None
        self._title = ""
        self._note = ""
        self._speech_warning = False
        self._status = "processing"
        self._expanded = 0.0
        self._collapsed = True
        self._listening = False
        self.setFixedHeight(44)
        self.setMinimumWidth(44)
        self.setMaximumWidth(44)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("查看通知中心")
        self.clicked.connect(self.open_notifications)
        self.animation = QPropertyAnimation(self, b"expansion", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.InOutCubic)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.collapse)

    @property
    def colors(self):
        return notification_style(self._theme_override)

    def _get_expansion(self):
        return self._expanded

    def _set_expansion(self, value):
        self._expanded = value
        self.setMaximumWidth(round(44 + 276 * value))
        self.updateGeometry()
        self.update()

    expansion = Property(float, _get_expansion, _set_expansion)

    def sizeHint(self):
        return QSize(round(44 + 276 * self._expanded), 44)

    def minimumSizeHint(self):
        return QSize(44, 44)

    def set_current(self, record=None, listening=False):
        self._listening = listening
        # A coarse Windows clock can give two different states the same time.
        # Compare the immutable record, not its timestamp alone.
        key = record if record else ("listening", listening)
        if key == self._record_key:
            if self._collapsed:
                self._show_idle()
            return
        self._record_key = key
        speech_note = SPEECH_NOTES.get(record.speech_status, "") if record else ""
        self._speech_warning = bool(speech_note)
        self._note = speech_note or ("你说：" + record.heard if record and record.heard else "")
        self._title = record.title if record else ("正在聆听" if listening else "语音未开启")
        self._status = record.status if record else "processing"
        if record and record.source == "microphone" and record.status == "success" and listening:
            self._title = "正在聆听"
            self._status = "processing"
        self.setText(self._title)  # Accessible text; painting is deliberately compact.
        tooltip = "\n".join(part for part in (
            self._title, "你说：" + record.heard if record and record.heard else "",
            speech_note, "点击查看通知中心") if part)
        self.setToolTip("<qt>" + escape(tooltip).replace("\n", "<br>") + "</qt>")
        self._collapsed = False
        self._animate(1.0)
        self.timer.stop()
        if record is None or record.phase == "done":
            self.timer.start(self.COLLAPSE_MS)

    def _animate(self, target):
        self.animation.stop()
        self.animation.setStartValue(self._expanded)
        self.animation.setEndValue(target)
        self.animation.start()

    def collapse(self):
        self._collapsed = True
        self._show_idle()
        self._animate(0.0)

    def _show_idle(self):
        self._title = "正在聆听" if self._listening else "语音未开启"
        self._note = ""
        self._speech_warning = False
        self._status = "processing"
        self.setText(self._title)
        self.setToolTip(self._title + "\n点击查看通知中心")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self.colors.secondary if self._collapsed and not self._listening else self.colors.color(self._status)
        painter.setPen(QPen(QColor(color), 1))
        painter.setBrush(QColor(self.colors.surface))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 21, 21)
        icon = "mic-off" if self._collapsed and not self._listening else status_icon(self._status)
        lucide_icon(icon, 18, color).paint(painter, 14, 13, 18, 18)
        if self.width() < 85:
            return
        text_rect = self.rect().adjusted(42, 5, -12, -4)
        painter.setPen(QColor(self.colors.text))
        painter.drawText(text_rect, Qt.AlignVCenter if not self._note else Qt.AlignTop,
                         painter.fontMetrics().elidedText(self._title, Qt.ElideRight, max(1, text_rect.width())))
        if self._note:
            font = QFont(painter.font())
            font.setPointSizeF(max(8, font.pointSizeF() - 1))
            painter.setFont(font)
            painter.setPen(QColor(self.colors.warning if self._speech_warning else self.colors.secondary))
            text_rect.setTop(23)
            painter.drawText(text_rect, Qt.AlignTop,
                             painter.fontMetrics().elidedText(self._note, Qt.ElideRight, max(1, text_rect.width())))
