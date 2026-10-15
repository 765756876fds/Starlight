"""电影面板：搜索 + 结果列表 + 播放控制（暂停/继续/进度条/全屏/倍速/音量/静音）。"""
from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from ..theme.colors import Colors
from ...movie.movie_controller import MovieController


class _SearchWorker(QObject):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, movie, keyword):
        super().__init__()
        self._movie = movie
        self._kw = keyword

    def run(self):
        try:
            out = self._movie.search(self._kw)
            self.done.emit(out or [])
        except Exception as e:
            self.failed.emit(str(e))


class MoviePanel(QWidget):
    def __init__(self, movie=None, music=None, parent=None):
        super().__init__(parent)
        self._movie = movie if movie is not None else MovieController(music=music)
        self._thread = None
        self._items_by_row = []

        root = QVBoxLayout(self)

        # 搜索行
        search_row = QHBoxLayout()
        self.edit_kw = QLineEdit()
        self.edit_kw.setPlaceholderText("输入片名搜索（如：流浪地球）")
        self.btn_search = QPushButton("搜索")
        self.btn_search.setProperty("class", "primary")
        self.btn_clear = QPushButton("清空")
        search_row.addWidget(self.edit_kw)
        search_row.addWidget(self.btn_search)
        search_row.addWidget(self.btn_clear)
        root.addLayout(search_row)

        # 状态
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        root.addWidget(self.lbl_status)

        # 结果列表
        self.list_results = QListWidget()
        root.addWidget(self.list_results, 1)

        # 当前播放
        self.lbl_now = QLabel("未播放")
        self.lbl_now.setAlignment(Qt.AlignCenter)
        root.addWidget(self.lbl_now)

        # 进度条
        self.slider_pos = QSlider(Qt.Horizontal)
        self.slider_pos.setRange(0, 1000)
        self.slider_pos.setEnabled(False)
        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setAlignment(Qt.AlignCenter)
        root.addWidget(self.slider_pos)
        root.addWidget(self.lbl_time)

        # 播放控制
        row = QHBoxLayout()
        self.btn_toggle = QPushButton("⏯")
        self.btn_stop = QPushButton("⏹")
        row.addWidget(self.btn_toggle)
        row.addWidget(self.btn_stop)
        row.addStretch()
        root.addLayout(row)

        # 增强控制行（--osc=no 后 mpv 窗口无控件，控制全在面板）
        row2 = QHBoxLayout()
        self.btn_fullscreen = QPushButton("⛶ 全屏")
        self.btn_speed = QPushButton("倍速 1x")
        self.lbl_vol = QLabel("音量")
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(70)
        self.slider_vol.setFixedWidth(120)
        self.btn_mute = QPushButton("🔊")
        self.btn_shortcut = QPushButton("快捷键")
        row2.addWidget(self.btn_fullscreen)
        row2.addWidget(self.btn_speed)
        row2.addWidget(self.lbl_vol)
        row2.addWidget(self.slider_vol)
        row2.addWidget(self.btn_mute)
        row2.addStretch()
        row2.addWidget(self.btn_shortcut)
        root.addLayout(row2)

        # 倍速档位（循环切换）
        self._speed_steps = [0.5, 1.0, 1.25, 1.5, 2.0]
        self._speed_idx = 1

        # 信号
        self.btn_search.clicked.connect(self._do_search)
        self.btn_clear.clicked.connect(self._do_clear)
        self.edit_kw.returnPressed.connect(self._do_search)
        self.list_results.itemDoubleClicked.connect(self._on_item_activated)
        self.btn_toggle.clicked.connect(self._on_toggle)
        self.btn_stop.clicked.connect(self._on_stop)
        self.slider_pos.sliderReleased.connect(self._on_seek)
        self.btn_fullscreen.clicked.connect(self._on_fullscreen)
        self.btn_speed.clicked.connect(self._on_speed)
        self.slider_vol.valueChanged.connect(self._on_volume)
        self.btn_mute.clicked.connect(self._on_mute)
        self.btn_shortcut.clicked.connect(self._on_shortcut)

        # 进度轮询
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # 软件退出钩子：杀掉电影 mpv（main.py 不感知 movie，单点收口）
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._on_app_quit)
        except Exception:
            pass

    def _on_app_quit(self):
        try:
            self._movie.close()
        except Exception:
            pass

    # ---- 搜索 ----

    def _do_search(self):
        kw = self.edit_kw.text().strip()
        if not kw:
            self.lbl_status.setText("请输入片名")
            return
        self.lbl_status.setText("搜索中...")
        self._thread = QThread()
        self._worker = _SearchWorker(self._movie, kw)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_search_done)
        self._worker.failed.connect(self._on_search_failed)
        self._worker.done.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    def _do_clear(self):
        self.edit_kw.clear()
        self.list_results.clear()
        self._items_by_row = []
        self.lbl_status.setText("")

    def _on_search_done(self, result: list):
        self.list_results.clear()
        self._items_by_row = []
        for it in result:
            name = it.get("vod_name", "") or ""
            remarks = it.get("vod_remarks", "") or ""
            label = f"{name}  [{remarks}]" if remarks else name
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, it)
            self.list_results.addItem(item)
            self._items_by_row.append(it)
        n = len(result)
        if n == 0:
            self.lbl_status.setText("无结果")
        else:
            self.lbl_status.setText(f"找到 {n} 部")

    def _on_search_failed(self, err: str):
        self.lbl_status.setText(f"搜索失败: {err}")

    # ---- 播放 ----

    def _on_item_activated(self, item):
        it = item.data(Qt.UserRole)
        if not isinstance(it, dict):
            return
        try:
            ok = self._movie.play(it)
            if ok:
                name = it.get("vod_name", "") or ""
                self.lbl_now.setText(f"播放中: {name}")
                self.lbl_status.setText("")
                self.slider_pos.setEnabled(True)
            else:
                self.lbl_status.setText("无法播放：无可用地址")
        except Exception as e:
            self.lbl_status.setText(f"播放失败: {e}")

    def _on_toggle(self):
        try:
            if self._movie.is_playing():
                self._movie.toggle_pause()
        except Exception as e:
            self.lbl_status.setText(f"控制失败: {e}")

    # ---- 增强控制 ----

    def _on_fullscreen(self):
        try:
            self._movie.set_fullscreen(True)
        except Exception as e:
            self.lbl_status.setText(f"全屏失败: {e}")

    def _on_speed(self):
        self._speed_idx = (self._speed_idx + 1) % len(self._speed_steps)
        rate = self._speed_steps[self._speed_idx]
        label = f"{rate:g}x" if rate != int(rate) else f"{int(rate)}x"
        self.btn_speed.setText(f"倍速 {label}")
        try:
            self._movie.set_speed(rate)
        except Exception as e:
            self.lbl_status.setText(f"倍速失败: {e}")

    def _on_volume(self, value):
        try:
            self._movie.set_volume(value)
        except Exception as e:
            self.lbl_status.setText(f"音量失败: {e}")

    def _on_mute(self):
        try:
            self._movie.toggle_mute()
            self.btn_mute.setText("🔇")
        except Exception as e:
            self.lbl_status.setText(f"静音失败: {e}")

    def _on_shortcut(self):
        table = (
            "mpv 快捷键（窗口内按键）\n"
            "====================\n"
            "空格     播放/暂停\n"
            "← / →    后退/前进 5 秒\n"
            "↑ / ↓    前进/后退 60 秒\n"
            "[ / ]    减速/加速 10%\n"
            "9 / 0    音量减/加\n"
            "m        静音\n"
            "f        全屏/退出全屏\n"
            "q        退出播放（关窗口）\n"
            "s        截图\n"
            "o        显示/隐藏 OSD 信息\n"
            "====================\n"
            "播放控制也可用本面板按钮（全屏/倍速/音量/静音）。"
        )
        QMessageBox.information(self, "快捷键说明", table)

    def _on_stop(self):
        try:
            self._movie.stop()
            self.lbl_now.setText("未播放")
            self.slider_pos.setEnabled(False)
            self.slider_pos.setValue(0)
            self.lbl_time.setText("0:00 / 0:00")
        except Exception as e:
            self.lbl_status.setText(f"停止失败: {e}")

    def _on_seek(self):
        try:
            if not self._movie.is_playing():
                return
            dur = self._movie.get_duration()
            if dur > 0:
                ratio = self.slider_pos.value() / 1000.0
                self._movie.seek(ratio * dur, mode="absolute")
        except Exception as e:
            self.lbl_status.setText(f"跳转失败: {e}")

    def _tick(self):
        try:
            if not self._movie.is_playing():
                return
            pos = self._movie.get_position()
            dur = self._movie.get_duration()
            if dur > 0:
                ratio = min(1.0, max(0.0, pos / dur))
                if not self.slider_pos.isSliderDown():
                    self.slider_pos.setValue(int(ratio * 1000))
                self.lbl_time.setText(f"{_fmt(pos)} / {_fmt(dur)}")
            else:
                self.lbl_time.setText(f"{_fmt(pos)} / 0:00")
        except Exception:
            pass


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"
