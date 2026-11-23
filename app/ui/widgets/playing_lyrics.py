"""Read-only playing lyrics view driven by the existing LyricsWindow clock."""
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from ..theme.tokens import Tokens
from .icon_utils import lucide_icon


class PlayingLyrics(QWidget):
    close_requested = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = None
        self.active_line = -1
        self._follow = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Tokens.SPACING_SM, Tokens.SPACING_SM, 0, 0)
        layout.setSpacing(Tokens.SPACING_SM)
        header = QHBoxLayout()
        self.tools = QHBoxLayout()
        self.title = QLabel("歌词")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setProperty("class", "title")
        self.title.setWordWrap(True)
        header.addWidget(self.title, 1)
        self.follow = QPushButton("当前行")
        self.follow.setAccessibleName("回到当前行")
        self.follow.setToolTip("回到当前行并继续跟随播放进度")
        self.follow.clicked.connect(self._resume_follow)
        self.tools.addWidget(self.follow)
        self.tools.addStretch()
        self.btn_close = QPushButton()
        self.btn_close.setFixedSize(32, 30)
        self.btn_close.setIcon(lucide_icon("x", 14, Tokens.TEXT_SECONDARY))
        self.btn_close.setAccessibleName("收起歌词")
        self.btn_close.setToolTip("收起歌词")
        self.btn_close.clicked.connect(self.close_requested.emit)
        header.addWidget(self.btn_close, 0, Qt.AlignTop)
        layout.addLayout(header)
        layout.addLayout(self.tools)
        self.message = QLabel("播放歌曲后显示歌词")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        layout.addWidget(self.message, 1)
        self.lines = QListWidget()
        self.lines.setWordWrap(True)
        self.lines.setSelectionMode(QAbstractItemView.NoSelection)
        self.lines.setFocusPolicy(Qt.StrongFocus)
        self.lines.viewport().installEventFilter(self)
        layout.addWidget(self.lines, 1)
        self.lines.verticalScrollBar().sliderPressed.connect(self._pause_follow)
        self.lines.verticalScrollBar().actionTriggered.connect(self._pause_follow)
        self.lines.itemClicked.connect(self._pause_follow)
        self.lines.hide()
        self.follow.hide()

    def attach(self, engine):
        self.engine = engine
        engine.lyrics_changed.connect(self.refresh)
        engine.appearance_changed.connect(self._restyle)
        engine.line_changed.connect(self.highlight)

    def refresh(self):
        if self.engine is None or self.isHidden():
            return
        self._follow = True
        self.active_line = -1
        self.lines.clear()
        status = self.engine.lyric_status
        title = self.engine.loading_title if status == "loading" else self.engine._title
        self.title.setText(title or "歌词")
        available = status == "ready"
        self.message.setVisible(not available)
        self.lines.setVisible(available)
        self.follow.setVisible(available)
        if available:
            for _, text in self.engine._lines:
                item = QListWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                self._style(item, False)
                self.lines.addItem(item)
            self.highlight(self.engine._current_idx)
        else:
            messages = {"idle": "播放歌曲后显示歌词", "loading": "正在查找歌词…", "missing": "这首歌暂无歌词"}
            self.message.setText(messages[status])

    def highlight(self, row):
        if self.isHidden() or not 0 <= row < self.lines.count():
            return
        previous = self.lines.item(self.active_line)
        if previous is not None:
            self._style(previous, False)
        self.active_line = row
        item = self.lines.item(row)
        self._style(item, True)
        if self._follow:
            self.lines.scrollToItem(item, QAbstractItemView.PositionAtCenter)

    def _style(self, item, active):
        font = item.font()
        size = int(self.engine._prefs.get("font_size") or 24)
        font.setPixelSize(max(12, round(size * 0.67)) + (3 if active else 0))
        font.setBold(active)
        item.setFont(font)
        item.setForeground(QBrush(QColor(Tokens.ACCENT)) if active else QBrush())

    def _restyle(self):
        if self.isHidden():
            return
        for row in range(self.lines.count()):
            self._style(self.lines.item(row), row == self.active_line)
        if self._follow:
            self.highlight(self.active_line)

    def _pause_follow(self, *_):
        self._follow = False

    def _resume_follow(self):
        self._follow = True
        self.highlight(self.active_line)

    def eventFilter(self, watched, event):
        if watched is self.lines.viewport() and event.type() == QEvent.Wheel:
            self._pause_follow()
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.StyleChange) and hasattr(self, "lines"):
            self.highlight(self.active_line)
