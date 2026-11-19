"""Favorite control for the song the music controller is currently showing."""
from PySide6.QtCore import QSize, Signal
from PySide6.QtWidgets import QPushButton, QToolTip

from ...music.database import liked_song_identity
from ..theme.tokens import Tokens
from .icon_utils import lucide_icon
from .latest_task import LatestTask


class CurrentSongLikeButton(QPushButton):
    changed = Signal()

    def __init__(self, music, parent=None):
        super().__init__(parent)
        self.setFixedSize(32, 30)
        self.setIconSize(QSize(17, 17))
        self.setCheckable(True)
        self._music = music
        self._closed = False
        self._song = None
        self._liked = None
        self._task = LatestTask(self)
        self._task.completed.connect(self._complete)
        self.clicked.connect(self._toggle)
        self._present("播放歌曲后可收藏", enabled=False)

    def sync(self):
        if self._closed:
            return
        current = getattr(self._music, "get_current_song", None)
        song = current() if current is not None else None
        if song is self._song:
            return
        self._clear_song()
        self._song = song
        if song is None:
            return
        if liked_song_identity(song) is None or not song.get("title"):
            self._present("这首歌缺少可用于收藏的标识", enabled=False)
            return
        self._read()

    def shutdown(self):
        self._closed = True
        self._clear_song()
        self._task.cancel()

    def _clear_song(self):
        self._task.invalidate()
        self._song = None
        self._liked = None
        self._present("播放歌曲后可收藏", enabled=False)

    def _read(self):
        self._present("正在读取收藏状态…", enabled=False)
        music, song = self._music, dict(self._song)
        self._task.submit(None, lambda: music.is_liked_song(song))

    def _toggle(self):
        # QPushButton toggles before clicked. Never show the new value until saved.
        self.setChecked(bool(self._liked))
        previous = self._song
        self.sync()
        if previous is not self._song or not self.isEnabled():
            return
        if self._liked is None:
            self._read()
            return
        desired = not self._liked
        music, song = self._music, dict(self._song)
        self._present("正在收藏…" if desired else "正在取消收藏…", enabled=False)
        self._task.submit(desired, lambda: music.set_liked_song(song, desired))

    def _complete(self, desired, result, error):
        previous = self._song
        self.sync()
        if previous is not self._song:
            return
        if desired is None and error is None and isinstance(result, bool):
            self._liked = result
        elif desired is not None and error is None and result is True:
            self._liked = desired
            self.changed.emit()
        else:
            message = ("无法读取收藏状态，点击重试" if desired is None
                       else "收藏未更新，点击重试")
            self._present(message)
            if self.isVisible() and desired is not None:
                QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()), message, self)
            return
        self._present("已收藏，点击取消收藏" if self._liked else "收藏到「我喜欢的音乐」")

    def _present(self, message, *, enabled=True):
        self.setChecked(bool(self._liked))
        color = Tokens.ACCENT if self._liked else Tokens.TEXT_SECONDARY
        self.setIcon(lucide_icon("heart", 17, color, filled=bool(self._liked)))
        self.setEnabled(enabled)
        self.setToolTip(message)
        self.setAccessibleName(message)
