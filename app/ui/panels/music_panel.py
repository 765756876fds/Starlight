"""音乐面板（绑定真实 MusicController）。"""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)


class MusicPanel(QWidget):
    def __init__(self, music=None, parent=None):
        super().__init__(parent)
        self._music = music
        layout = QVBoxLayout(self)

        self.lbl_track = QLabel("未播放")
        self.lbl_track.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_track)

        row = QHBoxLayout()
        self.btn_prev = QPushButton("⏮")
        self.btn_toggle = QPushButton("⏯")
        self.btn_next = QPushButton("⏭")
        for b in (self.btn_prev, self.btn_toggle, self.btn_next):
            row.addWidget(b)
        layout.addLayout(row)

        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        layout.addWidget(QLabel("音量"))
        layout.addWidget(self.slider_vol)

        layout.addStretch()

        if music is not None:
            self.btn_prev.clicked.connect(self._on_prev)
            self.btn_toggle.clicked.connect(self._on_toggle)
            self.btn_next.clicked.connect(self._on_next)
            self.slider_vol.valueChanged.connect(self._on_volume)
            # 初始音量
            try:
                self.slider_vol.setValue(int(music.get_volume() or 0))
            except Exception:
                pass

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

    def _on_prev(self):
        try:
            self._music.prev()
        except Exception:
            pass

    def _on_toggle(self):
        try:
            self._music.toggle()
        except Exception:
            pass

    def _on_next(self):
        try:
            self._music.next()
        except Exception:
            pass

    def _on_volume(self, v):
        try:
            self._music.set_volume(int(v))
        except Exception:
            pass

    def _refresh(self):
        if self._music is None:
            return
        try:
            song = self._music.get_current_song()
            if song:
                title = song.get("title") or song.get("name") or ""
                artist = song.get("artist") or ""
                self.lbl_track.setText(f"{title} - {artist}" if artist else title)
        except Exception:
            pass
