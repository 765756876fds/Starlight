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
        self._local_songs = []
        self._thread = None

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)

        # ---- 本地 Tab ----
        local = QWidget()
        ll = QVBoxLayout(local)

        # 搜索行
        local_search_row = QHBoxLayout()
        self.edit_local = QLineEdit()
        self.edit_local.setPlaceholderText("搜索本地歌曲")
        self.btn_local_search = QPushButton("搜索")
        self.btn_local_search.setProperty("class", "primary")
        self.btn_local_clear = QPushButton("清空")
        local_search_row.addWidget(self.edit_local)
        local_search_row.addWidget(self.btn_local_search)
        local_search_row.addWidget(self.btn_local_clear)
        ll.addLayout(local_search_row)

        # 本地列表
        self.list_local = QListWidget()
        ll.addWidget(self.list_local, 1)

        # 当前曲目
        self.lbl_track = QLabel("未播放")
        self.lbl_track.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.lbl_track)

        # 进度条
        self.slider_pos = QSlider(Qt.Horizontal)
        self.slider_pos.setRange(0, 1000)
        self.slider_pos.setEnabled(False)
        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.slider_pos)
        ll.addWidget(self.lbl_time)

        # 播放控制
        row = QHBoxLayout()
        self.btn_prev = QPushButton("⏮")
        self.btn_toggle = QPushButton("⏯")
        self.btn_next = QPushButton("⏭")
        for b in (self.btn_prev, self.btn_toggle, self.btn_next):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(QLabel("音量"))
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        row.addWidget(self.slider_vol, 1)
        ll.addLayout(row)

        # 高潮模式
        hrow = QHBoxLayout()
        self.btn_highlight = QPushButton("🎵 高潮模式：关")
        self.btn_highlight.setCheckable(True)
        self.btn_highlight.toggled.connect(self._on_highlight)
        self.btn_mark = QPushButton("📍 标记高潮起点")
        self.btn_mark.clicked.connect(self._on_mark_highlight)
        hrow.addWidget(self.btn_highlight)
        hrow.addWidget(self.btn_mark)
        ll.addLayout(hrow)

        tabs.addTab(local, "本地")

        # ---- 在线 Tab ----
        online = QWidget()
        ol = QVBoxLayout(online)
        search_row = QHBoxLayout()
        self.edit_kw = QLineEdit()
        self.edit_kw.setPlaceholderText("搜索在线音乐")
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
            self.slider_pos.sliderReleased.connect(self._on_seek)
            self.btn_search.clicked.connect(self._do_search)
            self.edit_kw.returnPressed.connect(self._do_search)
            self.list_results.itemDoubleClicked.connect(self._on_play)
            self.btn_local_search.clicked.connect(self._do_local_search)
            self.edit_local.returnPressed.connect(self._do_local_search)
            self.btn_local_clear.clicked.connect(self._load_local_all)
            self.list_local.itemDoubleClicked.connect(self._on_local_play)
            try:
                self.slider_vol.setValue(int(music.get_volume() or 0))
            except Exception:
                pass
            try:
                for name in (music.get_online_sources() or []):
                    self.cb_source.addItem(name, userData=name)
            except Exception:
                pass
            # 初始加载全部
            self._load_local_all()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

    # ---- 本地 ----
    def _load_local_all(self):
        self.edit_local.setText("")
        self._refresh_local_list("")

    def _do_local_search(self):
        kw = self.edit_local.text().strip()
        self._refresh_local_list(kw)

    def _refresh_local_list(self, kw: str):
        if self._music is None:
            return
        try:
            songs = self._music.search_local(kw)
        except Exception as e:
            print(f"[Music] search_local error: {e}")
            songs = []
        self._local_songs = songs
        self.list_local.clear()
        for s in songs:
            title = s.get("title", "?")
            artist = s.get("artist", "")
            item = QListWidgetItem(f"{title} - {artist}")
            item.setData(Qt.UserRole, s)
            self.list_local.addItem(item)

    def _on_local_play(self, item):
        s = item.data(Qt.UserRole)
        if not s or self._music is None:
            return
        print(f"[UI] local play: {s.get('title')} id={s.get('id')}")
        try:
            self._music.play_song(s)
        except Exception as e:
            print(f"[UI] local play error: {e}")

    def _on_mark_highlight(self):
        if self._music is None:
            return
        ok = False
        try:
            ok = self._music.mark_highlight_start()
        except Exception as e:
            print(f"[Music] mark error: {e}")
        if ok:
            self.lbl_track.setText("已标记当前歌曲高潮段")

    def _on_highlight(self, on: bool):
        self.btn_highlight.setText(f"🎵 高潮模式：{'开' if on else '关'}")
        if self._music is not None:
            try:
                if hasattr(self._music, "set_highlight_mode"):
                    self._music.set_highlight_mode(on)
            except Exception as e:
                print(f"[Music] highlight toggle: {e}")

    # ---- 播放控制 ----
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

    # ---- 在线 ----
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
                item = QListWidgetItem(f"[{src}] {t.title} - {t.artist}")
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

    def _on_seek(self):
        if self._music is None:
            return
        try:
            dur = self._music.mpv.get_duration() if hasattr(self._music, "mpv") else 0
            if dur > 0:
                frac = self.slider_pos.value() / 1000.0
                self._music.mpv.seek(frac * dur)
        except Exception:
            pass

    def _refresh(self):
        if self._music is None:
            return
        try:
            if hasattr(self._music, "mpv"):
                pos = self._music.mpv.get_live_playback_time()
                dur = self._music.mpv.get_duration()
                if dur > 0:
                    self.slider_pos.setEnabled(True)
                    if not self.slider_pos.isSliderDown():
                        self.slider_pos.setValue(int(pos / dur * 1000))
                    self.lbl_time.setText(
                        f"{int(pos//60)}:{int(pos%60):02d} / {int(dur//60)}:{int(dur%60):02d}")
            song = self._music.get_current_song()
            if song:
                title = song.get("title") or song.get("name") or ""
                artist = song.get("artist") or ""
                prefix = "[在线] " if song.get("online") else ""
                text = f"{prefix}{title} - {artist}" if artist else f"{prefix}{title}"
                self.lbl_track.setText(text)
        except Exception:
            pass
