"""Shared UI conventions for music and film. Playback decisions stay with each panel."""
from PySide6.QtWidgets import QHBoxLayout, QWidget

from ..theme.tokens import Tokens


SEARCHING = "正在搜索…"
SEARCH_RETRY = "搜索暂未完成，请重试。"
PLAY_LOADING = "正在加载…"
PLAY_STARTED = "正在播放"
PLAY_RETRY = "暂时无法播放，请重试或换一个版本。"


class FilterRow(QWidget):
    """Keep optional filters below the search field and show when any are active."""

    def __init__(self, button, *fields):
        super().__init__()
        self.button = button
        self.fields = fields
        button.setCheckable(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Tokens.SPACING_SM)
        for field in fields:
            row.addWidget(field)
            field.currentIndexChanged.connect(self.update_summary)
        row.addStretch()
        button.toggled.connect(self.set_expanded)
        self.set_expanded(False)
        self.update_summary()

    def set_expanded(self, expanded):
        for field in self.fields:
            field.setVisible(expanded)
        self.setVisible(expanded)

    def update_summary(self, *_):
        count = sum(field.currentIndex() > 0 and field.isEnabled() for field in self.fields)
        self.button.setText(f"筛选（{count}）" if count else "筛选")
