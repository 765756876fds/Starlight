"""On-demand popular songs view; source browsing and playback remain in MusicPanel."""
from collections import deque
from datetime import datetime
from functools import partial
from time import monotonic

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QPushButton

from ...music.online.popular_charts import chart_sources, group_charts, read_chart, version_label
from .discovery_cards import CardList, label
from .latest_task import LatestTask


class PopularSongs(QWidget):
    play_requested = Signal(object, int)

    def __init__(self, music, parent=None):
        super().__init__(parent)
        self.music = music
        self.tasks = [LatestTask(self, timeout_ms=42000) for _ in range(2)]
        self.cache = {}
        self.queue = deque()
        self.pending = set()
        self.failed = set()
        self.sources = []
        self.closed = False
        for task in self.tasks:
            task.completed.connect(partial(self._received, task))
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        title = label("多榜热门")
        title.setProperty("class", "title")
        head.addWidget(title, 1)
        self.refresh = QPushButton("刷新")
        self.refresh.clicked.connect(lambda: self.load(force=True))
        head.addWidget(self.refresh)
        layout.addLayout(head)
        self.cards = CardList(self)
        self.cards.currentItemChanged.connect(self._selected)
        self.cards.itemDoubleClicked.connect(self._play)
        layout.addWidget(self.cards, 1)
        self.status = label("", True)
        layout.addWidget(self.status)
        self.play_status = label("", True)
        layout.addWidget(self.play_status)
        footer = QHBoxLayout()
        self.versions = QComboBox()
        self.versions.setAccessibleName("歌曲版本与榜单出处")
        footer.addWidget(self.versions, 1)
        self.play = QPushButton("播放")
        self.play.clicked.connect(self._play)
        footer.addWidget(self.play)
        layout.addLayout(footer)
        self._selected(None)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.closed:
            self.load()

    def hideEvent(self, event):
        self._invalidate()
        super().hideEvent(event)

    def _invalidate(self):
        self.queue.clear()
        self.pending.clear()
        for task in self.tasks:
            task.invalidate()

    def shutdown(self):
        self.closed = True
        self._invalidate()
        for task in self.tasks:
            task.cancel()

    def load(self, *, force=False):
        self._invalidate()
        self.failed.clear()
        plugins = self.music.discovery_sources() if callable(getattr(self.music, "discovery_sources", None)) else []
        self.sources = chart_sources(plugins or [])
        self.cache = {s: v for s, v in self.cache.items() if s in self.sources}
        for source in self.sources:
            if force or source not in self.cache or monotonic() - self.cache[source][0] >= 120:
                self.queue.append(source)
        self.pending.update(self.queue)
        self._render()
        for task in self.tasks:
            self._next(task)

    def _next(self, task):
        if self.queue:
            source = self.queue.popleft()
            task.submit(source, partial(read_chart, self.music, source), guarded=True)

    def _received(self, task, source, entries, error):
        self.pending.discard(source)
        if error:
            self.failed.add(source)
        else:
            self.cache[source] = (monotonic(), datetime.now(), entries)
        self._render()
        self._next(task)

    def _render(self):
        item = self.cards.currentItem()
        previous = item.data(Qt.UserRole).entries[self.versions.currentIndex()].track if item else None
        batches = [self.cache[s][2] for s in self.sources if s in self.cache]
        groups = group_charts(batches)
        def describe(group):
            track = group.entries[0].track
            return dict(title=track.title, subtitle=track.artist, cover_url=track.cover_url,
                        caption=" · ".join(filter(None, (version_label(track), track.album))))
        self.cards.set_rows(groups, describe)
        if previous:
            for i, group in enumerate(groups):
                match = next((n for n, e in enumerate(group.entries) if
                              (e.source, e.track.track_id) == (previous.source_name, previous.track_id)), None)
                if match is not None:
                    self.cards.setCurrentRow(i)
                    self.versions.setCurrentIndex(match)
                    break
        states = []
        if groups:
            stamp = min(self.cache[s][1] for s in self.sources if s in self.cache)
            states.append(f"{len(groups)} 首 · 更新于 {stamp:%m-%d %H:%M}")
        if self.pending:
            states.append("正在读取热门歌曲…")
        if self.failed:
            states.append("部分榜单未能更新" if groups else "榜单未能更新，可刷新或按来源查看")
        if self.failed.intersection(self.cache) or any(
                monotonic() - self.cache[s][0] >= 120 for s in self.pending if s in self.cache):
            states.append("含上次缓存")
        self.status.setText(" · ".join(states) or "暂无热门歌曲，可按来源查看其他榜单")

    def _selected(self, item, *_):
        self.versions.clear()
        if item:
            for entry in item.data(Qt.UserRole).entries:
                t = entry.track
                self.versions.addItem(" · ".join(filter(None, (t.source_name, version_label(t), t.album, entry.board,
                                                              f"第 {entry.rank} 名"))))
        self.versions.setVisible(item is not None)
        self.play.setVisible(item is not None)

    def _play(self, *_):
        row = self.cards.currentRow()
        version = self.versions.currentIndex()
        if row < 0 or version < 0:
            return
        tracks = [self.cards.item(i).data(Qt.UserRole).entries[version if i == row else 0].track
                  for i in range(self.cards.count())]
        self.play_requested.emit(tracks, row)
