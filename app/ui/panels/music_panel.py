"""音乐面板（本地 + 在线）。"""
from PySide6.QtCore import QThread, Qt, QTimer, QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSlider, QTabWidget, QVBoxLayout, QWidget,
)

from ..theme.colors import Colors
from ...core.event_bus import get_event_bus
from ...music.online.events import ONLINE_SEARCH_DONE, ONLINE_ERROR


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


class _ScanWorker(QObject):
    """后台扫描本地音乐（复用 main 启动时的线程化 rescan 模式）。"""
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, music):
        super().__init__()
        self._music = music

    def run(self):
        try:
            result = self._music.rescan()
            self.done.emit(result or {})
        except Exception as e:
            self.failed.emit(str(e))


class MusicPanel(QWidget):
    # P1 候选旁路：后台线程 publish → 此 Signal 桥到主线程渲染
    _search_event = Signal(dict)

    def __init__(self, music=None, parent=None):
        super().__init__(parent)
        self._music = music
        self._tracks_by_row = []
        self._local_songs = []
        self._thread = None
        self._scan_thread = None
        self._voice_generation = None   # P1 候选旁路：当前 UI 展示的语音候选代际

        root = QVBoxLayout(self)
        tabs = self.tabs = QTabWidget()
        root.addWidget(tabs)

        # ---- 本地 Tab ----
        local = QWidget()
        ll = QVBoxLayout(local)

        # 扫描行（顶部）
        scan_row = QHBoxLayout()
        self.btn_scan_local = QPushButton("扫描本地音乐")
        self.btn_scan_local.clicked.connect(self._on_scan_local)
        scan_row.addWidget(self.btn_scan_local)
        self.lbl_scan_status = QLabel("")
        self.lbl_scan_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        scan_row.addWidget(self.lbl_scan_status)
        scan_row.addStretch()
        ll.addLayout(scan_row)

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
        self.btn_mode = QPushButton("🔁 顺序")
        self.btn_clear_queue = QPushButton("🗑 清空队列")
        self._modes = [
            ("sequential", "🔁 顺序"),
            ("shuffle", "🔀 随机"),
            ("repeat_all", "🔁 列表循环"),
            ("repeat_one", "🔂 单曲循环"),
        ]
        self._mode_idx = 0
        for b in (self.btn_prev, self.btn_toggle, self.btn_next, self.btn_mode,
                  self.btn_clear_queue):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(QLabel("音量"))
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        row.addWidget(self.slider_vol, 1)
        ll.addLayout(row)

        # 高潮模式开关
        hrow = QHBoxLayout()
        self.btn_highlight = QPushButton("🎵 高潮模式：关")
        self.btn_highlight.setCheckable(True)
        self.btn_highlight.toggled.connect(self._on_highlight)
        hrow.addWidget(self.btn_highlight)
        hrow.addStretch()
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
            self.btn_mode.clicked.connect(self._on_toggle_mode)
            self.btn_clear_queue.clicked.connect(self._on_clear_queue)
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

        # P1 候选旁路：订阅 controller 推送的候选事件（Signal 桥到主线程）
        self._search_event.connect(self._on_search_show)
        try:
            get_event_bus().subscribe(ONLINE_SEARCH_DONE, self._on_search_event)
            get_event_bus().subscribe(ONLINE_ERROR, self._on_error_event)
        except Exception as e:
            print(f"[MusicPanel] 订阅候选事件失败: {e}")

    # ---- 本地 ----
    def _on_scan_local(self):
        """扫描本地音乐：读 music_dirs.json 启用目录 → 应用到 scanner → QThread rescan。"""
        if self._music is None:
            return
        from ..settings_store import load_music_dirs, ALL_EXTENSIONS
        dirs = [d for d in load_music_dirs() if d.get("enabled")]
        if not dirs:
            self.lbl_scan_status.setText("无启用的音乐目录，请到设置中添加")
            return
        exts = set()
        for d in dirs:
            for e in (d.get("extensions") or []):
                e = str(e).lower().lstrip(".")
                if e:
                    exts.add("." + e)
        if not exts:
            exts = {"." + e for e in ALL_EXTENSIONS}
        try:
            self._music.scanner.scan_dirs = [d["path"] for d in dirs]
            self._music.scanner.extensions = exts
        except Exception as e:
            self.lbl_scan_status.setText(f"应用扫描目录失败: {e}")
            return
        self.btn_scan_local.setEnabled(False)
        self.lbl_scan_status.setText("扫描中…")
        self._scan_thread = QThread()
        self._scan_worker = _ScanWorker(self._music)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.done.connect(self._scan_thread.quit)
        self._scan_worker.failed.connect(self._scan_thread.quit)
        self._scan_thread.start()

    def _on_scan_done(self, result: dict) -> None:
        self.btn_scan_local.setEnabled(True)
        self.lbl_scan_status.setText(
            f"扫描完成：新增{result.get('added', 0)} 更新{result.get('updated', 0)} "
            f"跳过{result.get('skipped', 0)} 错误{result.get('errors', 0)}")
        self._load_local_all()

    def _on_scan_failed(self, err: str) -> None:
        self.btn_scan_local.setEnabled(True)
        self.lbl_scan_status.setText(f"扫描失败: {err}")

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

    def _on_toggle_mode(self):
        self._mode_idx = (self._mode_idx + 1) % len(self._modes)
        mode, label = self._modes[self._mode_idx]
        self.btn_mode.setText(label)
        if self._music:
            self._music.set_play_mode(mode)

    def _on_clear_queue(self):
        """清空队列：确认后停止播放 + 清队列（防误触）。"""
        if self._music is None:
            return
        ret = QMessageBox.question(
            self, "清空队列",
            "确定清空队列并停止当前播放？\n（此操作不可撤销）",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self._music.clear_queue()
        except Exception as e:
            print(f"[MusicPanel] 清空队列失败: {e}")

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
        self._voice_generation = None   # 手动搜索 → 重置语音候选代际
        self.list_results.clear()
        self._tracks_by_row = []
        source_filter = self.cb_source.currentData()
        n = 0
        for src, tracks in result.items():
            if source_filter and src != source_filter:
                continue
            for t in tracks:
                # 显示具体插件源名（t.source_name = 插件 platform，如"酷我音乐"），
                # 而不是 dict key "js_bridge"（7 个 JS 插件合并注册成一个源）
                item = QListWidgetItem(f"[{t.source_name}] {t.title} - {t.artist}")
                self.list_results.addItem(item)
                self._tracks_by_row.append(t)
                n += 1
        self.lbl_status.setText(f"已找到 {n} 首")

    def _on_search_failed(self, err: str):
        self.lbl_status.setText(f"搜索失败: {err}")

    def _on_search_event(self, event):
        """后台线程 publish → Signal 桥到主线程（Qt 队列连接自动跨线程）。"""
        try:
            self._search_event.emit(dict(event.data or {}))
        except Exception as e:
            print(f"[MusicPanel] 候选事件桥接失败: {e}")

    def _on_search_show(self, payload: dict):
        """主线程：渲染语音候选到在线 tab（不改变当前播放）。"""
        try:
            gen = payload.get("generation")
            display = payload.get("display") or []
            if gen is None or not isinstance(display, list):
                return
            gen = int(gen)
            if self._voice_generation is not None and gen <= self._voice_generation:
                return
            self._voice_generation = gen
            try:
                if hasattr(self, "tabs"):
                    self.tabs.setCurrentIndex(1)
            except Exception:
                pass
            self.list_results.clear()
            self._tracks_by_row = []
            for row in display:
                src = row.get("source", "")
                title = row.get("title", "")
                artist = row.get("artist", "")
                item = QListWidgetItem(f"[{src}] {title} - {artist}")
                self.list_results.addItem(item)
            n = len(display)
            if n:
                self.lbl_status.setText(f"共找到 {n} 首候选，双击播放")
            else:
                self.lbl_status.setText("未找到候选")
        except Exception as e:
            print(f"[MusicPanel] 候选渲染失败: {e}")

    def _on_error_event(self, event):
        """换源全部失败 -> 状态栏提示（不跳下一首）。"""
        try:
            msg = (event.data or {}).get("message", "在线播放失败")
            self.lbl_status.setText(msg)
        except Exception as e:
            print(f"[MusicPanel] 错误事件处理失败: {e}")

    def _on_play(self, item: QListWidgetItem):
        row = self.list_results.row(item)
        if row < 0:
            return
        # P1 候选换源：语音候选列表（_voice_generation 非 None）→ 换源续播
        if self._voice_generation is not None:
            try:
                if self._music.play_online_resume(self._voice_generation, row):
                    self.lbl_status.setText("正在换源…")
                    return
            except Exception as e:
                self.lbl_status.setText(f"换源失败: {e}")
                return
        # 手动搜索路径（原样）
        if row >= len(self._tracks_by_row):
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
                # 自动定位到本地列表中正在播放的歌曲
                if not song.get("online"):
                    song_id = song.get("id")
                    if song_id:
                        for i in range(self.list_local.count()):
                            item = self.list_local.item(i)
                            s = item.data(Qt.UserRole)
                            if s and s.get("id") == song_id:
                                self.list_local.setCurrentRow(i)
                                self.list_local.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                                break
        except Exception as e:
            print(f"[UI] music refresh error: {e!r}")
        # 同步播放模式/高潮模式按钮：独立 try，不被上面的进度/歌曲异常拖累
        try:
            self._sync_state_buttons()
        except Exception as e:
            print(f"[UI] sync buttons error: {e!r}")

    def _sync_state_buttons(self):
        """从 controller 实时状态同步按钮显示：
        语音「随机播放/顺序播放/高潮模式开/关」等命令改变 controller 状态后，
        面板按钮必须跟随（不能只依赖用户点击更新）。"""
        if self._music is None:
            return
        try:
            mode = self._music.get_play_mode()
        except Exception:
            mode = None
        if mode:
            for i, (m, label) in enumerate(self._modes):
                if m == mode:
                    if self._mode_idx != i:
                        self._mode_idx = i
                    if self.btn_mode.text() != label:
                        self.btn_mode.setText(label)
                        print(f"[UI] 播放模式按钮同步: {label}")
                    break
        try:
            hl = bool(self._music.is_highlight_mode())
        except Exception:
            hl = None
        if hl is not None:
            label = "🎵 高潮模式：开" if hl else "🎵 高潮模式：关"
            if self.btn_highlight.isChecked() != hl:
                self.btn_highlight.setChecked(hl)
            if self.btn_highlight.text() != label:
                self.btn_highlight.setText(label)
                print(f"[UI] 高潮模式按钮同步: {label}")
