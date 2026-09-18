"""音乐面板（本地 + 在线）。"""
from PySide6.QtCore import QThread, Qt, QTimer, QObject, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSlider, QTabWidget, QVBoxLayout, QWidget,
)


class _SearchWorker(QObject):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, music, keyword):
        super().__init__()
        self._music = music
        self._kw = keyword

    def run(self):
        try:
            out = self._music.search_online(self._kw)
            self.done.emit(out)
        except Exception as e:
            self.failed.emit(str(e))


class MusicPanel(QWidget):
    def __init__(self, music=None, parent=None):
        super().__init__(parent)
        self._music = music
        self._tracks_by_row = []
        self._thread = None

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)

        # ---- 本地 Tab ----
        local = QWidget()
        ll = QVBoxLayout(local)
        self.lbl_track = QLabel("未播放")
        self.lbl_track.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.lbl_track)
        row = QHBoxLayout()
        self.btn_prev = QPushButton("⏮")
        self.btn_toggle = QPushButton("⏯")
        self.btn_next = QPushButton("⏭")
        for b in (self.btn_prev, self.btn_toggle, self.btn_next):
            row.addWidget(b)
        ll.addLayout(row)
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        ll.addWidget(QLabel("音量"))
        ll.addWidget(self.slider_vol)
        ll.addStretch()
        tabs.addTab(local, "本地")

        # ---- 在线 Tab ----
        online = QWidget()
        ol = QVBoxLayout(online)
        search_row = QHBoxLayout()
        self.edit_kw = QLineEdit()
        self.edit_kw.setPlaceholderText("搜索在线音乐（archive.org）")
        self.btn_search = QPushButton("搜索")
        self.btn_search.setProperty("class", "primary")
        self.cb_source = QComboBox()
        self.cb_source.addItem("全部", userData=None)
        search_row.addWidget(self.edit_kw)
        search_row.addWidget(self.cb_source)
        search_row.addWidget(self.btn_search)
        ol.addLayout(search_row)
        self.list_results = QListWidget()
        ol.addWidget(self.list_results)
        self.lbl_status = QLabel("输入关键词开始搜索")
        ol.addWidget(self.lbl_status)
        tabs.addTab(online, "在线")

        # 绑定
        if music is not None:
            self.btn_prev.clicked.connect(self._on_prev)
            self.btn_toggle.clicked.connect(self._on_toggle)
            self.btn_next.clicked.connect(self._on_next)
            self.slider_vol.valueChanged.connect(self._on_volume)
            self.btn_search.clicked.connect(self._do_search)
            self.edit_kw.returnPressed.connect(self._do_search)
            self.list_results.itemDoubleClicked.connect(self._on_play)
            try:
                self.slider_vol.setValue(int(music.get_volume() or 0))
            except Exception:
                pass
            # 填充 source 列表
            try:
                for name in (music.get_online_sources() or []):
                    self.cb_source.addItem(name, userData=name)
            except Exception:
                pass

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

    def _on_prev(self):
        try: self._music.prev()
        except Exception: pass

    def _on_toggle(self):
        try: self._music.toggle()
        except Exception: pass

    def _on_next(self):
        try: self._music.next()
        except Exception: pass

    def _on_volume(self, v):
        try: self._music.set_volume(int(v))
        except Exception: pass

    def _do_search(self):
        kw = self.edit_kw.text().strip()
        if not kw or self._music is None:
            return
        self.lbl_status.setText("搜索中...")
        self._thread = QThread()
        self._worker = _SearchWorker(self._music, kw)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_search_done)
        self._worker.failed.connect(self._on_search_failed)
        self._worker.done.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    def _on_search_done(self, result: dict):
        self.list_results.clear()
        self._tracks_by_row = []
        source_filter = self.cb_source.currentData()
        n = 0
        for src, tracks in result.items():
            if source_filter and src != source_filter:
                continue
            for t in tracks:
                item = QListWidgetItem(f"{t.title} - {t.artist} [{src}]")
                self.list_results.addItem(item)
                self._tracks_by_row.append(t)
                n += 1
        self.lbl_status.setText(f"已找到 {n} 首")

    def _on_search_failed(self, err: str):
        self.lbl_status.setText(f"搜索失败: {err}")

    def _on_play(self, item: QListWidgetItem):
        row = self.list_results.row(item)
        if row < 0 or row >= len(self._tracks_by_row):
            return
        track = self._tracks_by_row[row]
        try:
            ok = self._music.play_online(track)
        except Exception as e:
            ok = False
            self.lbl_status.setText(f"播放失败: {e}")
            return
        if ok:
            self.lbl_status.setText(f"正在播放：{track.title}")
        else:
            self.lbl_status.setText("该曲目无法播放")

    def _refresh(self):
        if self._music is None:
            return
        try:
            song = self._music.get_current_song()
            if song:
                title = song.get("title") or song.get("name") or ""
                artist = song.get("artist") or ""
                prefix = "[在线] " if song.get("online") else ""
                text = f"{prefix}{title} - {artist}" if artist else f"{prefix}{title}"
                self.lbl_track.setText(text)
        except Exception:
            pass
