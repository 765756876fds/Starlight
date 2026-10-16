"""电影面板：搜索 + 结果列表 + 播放控制（暂停/继续/进度条/全屏/倍速/音量/静音）
+ 剧集选集 + 自动连播 + Assrt 字幕。"""
from pathlib import Path
import html as _html_mod
import re
import urllib.request

from PySide6.QtCore import QObject, QSize, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListView, QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from ..dialogs.movie_source_dialog import MovieSourceDialog

# ---- 海报缓存（复用 overlay_assets 模式：磁盘缓存 + 模块级内存 dict） ----
_POSTER_DIR = Path("data/movie/posters")
_POSTER_DIR.mkdir(parents=True, exist_ok=True)
_POSTER_SIZE = (140, 210)  # 竖版 2:3（对齐爱奇艺 PC 海报墙）
_posters_cache: dict = {}


def _strip_html(text):
    """轻量剥 HTML 标签（vod_content 是富文本简介）+ 反转义。"""
    if not text:
        return ""
    s = re.sub(r"<[^>]+>", "", str(text))
    s = _html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _make_poster_placeholder(name):
    """灰底占位图（下载失败/无海报时显示片名）。"""
    pix = QPixmap(*_POSTER_SIZE)
    pix.fill(QColor(60, 62, 70))
    p = QPainter(pix)
    p.setPen(QColor(210, 210, 210))
    p.setFont(QFont("Microsoft YaHei", 11))
    p.drawText(pix.rect(), Qt.AlignCenter | Qt.TextWordWrap, (name or "无海报")[:12])
    p.end()
    return pix


def _get_poster(vod_id):
    """内存 → 磁盘缓存命中返回 QPixmap；未下载返回 None（调用方放占位 + 异步下载）。"""
    if not vod_id:
        return None
    if vod_id in _posters_cache:
        return _posters_cache[vod_id]
    path = _POSTER_DIR / f"{vod_id}.jpg"
    if path.exists():
        pix = QPixmap(str(path))
        if not pix.isNull():
            _posters_cache[vod_id] = pix
            return pix
    return None

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


class _PosterBatchWorker(QObject):
    """串行下载整批海报到磁盘缓存；逐张 done(vod_id, 本地路径)（失败=""）。"""

    done = Signal(str, str)

    def __init__(self, jobs):
        super().__init__()
        self._jobs = jobs  # [(vod_id, url)]

    def run(self):
        for vod_id, url in self._jobs:
            path = _POSTER_DIR / f"{vod_id}.jpg"
            try:
                if not path.exists():
                    urllib.request.urlretrieve(url, path)
                if path.exists():
                    self.done.emit(str(vod_id), str(path))
                    continue
            except Exception as e:
                print(f"[Movie] 海报下载失败 {vod_id}: {e}")
            self.done.emit(str(vod_id), "")


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
        self.btn_view = QPushButton("海报")
        self.btn_view.setCheckable(True)
        self.btn_view.setChecked(True)
        self.btn_view.setToolTip("列表/海报 视图切换（默认海报墙，对齐爱奇艺/B站）")
        search_row.addWidget(self.btn_view)
        self.btn_source = QPushButton("源")
        self.btn_source.setToolTip("电影源管理（多源 / 测源 / 增删 / 设当前）")
        search_row.addWidget(self.btn_source)
        root.addLayout(search_row)

        # 状态行：搜索状态 + 当前源
        status_row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        self.lbl_source = QLabel("")
        self.lbl_source.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        status_row.addWidget(self.lbl_status, 1)
        status_row.addWidget(self.lbl_source)
        root.addLayout(status_row)

        # 结果区：列表/海报 双视图（QStackedWidget，默认海报墙）
        self.stack_results = QStackedWidget()
        self.list_results = QListWidget()
        self.list_posters = QListWidget()
        self.list_posters.setViewMode(QListWidget.IconMode)
        self.list_posters.setIconSize(QSize(*_POSTER_SIZE))
        self.list_posters.setGridSize(QSize(170, 250))
        self.list_posters.setResizeMode(QListView.Adjust)
        self.list_posters.setSpacing(10)
        self.list_posters.setWordWrap(True)
        self.stack_results.addWidget(self.list_results)
        self.stack_results.addWidget(self.list_posters)
        root.addWidget(self.stack_results, 1)
        self._poster_mode = True
        self.stack_results.setCurrentWidget(self.list_posters)  # 默认海报墙

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
        self.lbl_res = QLabel("")
        self.lbl_res.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        time_row = QHBoxLayout()
        time_row.addWidget(self.lbl_time, 1)
        time_row.addWidget(self.lbl_res)
        root.addWidget(self.slider_pos)
        root.addLayout(time_row)

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
        self.btn_subtitle = QPushButton("字")
        self.btn_shortcut = QPushButton("快捷键")
        self.cb_autonext = QCheckBox("连播")
        self.cb_autonext.setToolTip("播完一集自动播下一集（末集停，不循环）")
        self.cb_autonext.setChecked(self._movie.is_auto_next())
        self.cb_autonext.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        row2.addWidget(self.btn_fullscreen)
        row2.addWidget(self.btn_speed)
        row2.addWidget(self.lbl_vol)
        row2.addWidget(self.slider_vol)
        row2.addWidget(self.btn_mute)
        row2.addWidget(self.btn_subtitle)
        row2.addWidget(self.cb_autonext)
        row2.addStretch()
        row2.addWidget(self.btn_shortcut)
        root.addLayout(row2)

        # 倍速档位（B站/爱奇艺标准 9 档；点按钮弹菜单选档，非循环点击）
        self._speed_steps = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
        self._speed_idx = 2  # 1.0x 为默认档

        # 信号
        self.btn_search.clicked.connect(self._do_search)
        self.btn_clear.clicked.connect(self._do_clear)
        self.edit_kw.returnPressed.connect(self._do_search)
        self.list_results.itemDoubleClicked.connect(self._on_item_activated)
        self.list_results.itemClicked.connect(self._on_item_clicked)
        self.list_posters.itemClicked.connect(self._on_item_clicked)
        self.list_posters.itemDoubleClicked.connect(self._on_item_activated)
        self.btn_view.clicked.connect(self._on_toggle_view)
        self.btn_source.clicked.connect(self._on_source_manage)
        self.btn_toggle.clicked.connect(self._on_toggle)
        self.btn_stop.clicked.connect(self._on_stop)
        self.slider_pos.sliderReleased.connect(self._on_seek)
        self.btn_fullscreen.clicked.connect(self._on_fullscreen)
        self.btn_speed.clicked.connect(self._on_speed)
        self.slider_vol.valueChanged.connect(self._on_volume)
        self.btn_mute.clicked.connect(self._on_mute)
        self.btn_subtitle.clicked.connect(self._on_subtitle)
        self.cb_autonext.toggled.connect(self._movie.set_auto_next)
        self.btn_shortcut.clicked.connect(self._on_shortcut)

        # 进度轮询
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # 单击开详情（300ms 延迟防双击误触：双击先触发一次单击，让位给播放）
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(300)
        self._click_timer.timeout.connect(self._on_click_timeout)
        self._pending_click_item = None

        # 当前源显示（源管理变更后刷新）
        self._refresh_source_label()

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
        self.list_posters.clear()
        self._items_by_row = []
        self.lbl_status.setText("")

    def _on_search_done(self, result: list):
        self.list_results.clear()
        self.list_posters.clear()
        self._items_by_row = []
        poster_jobs = []
        for it in result:
            name = it.get("vod_name", "") or ""
            remarks = it.get("vod_remarks", "") or ""
            # 列表视图
            label = f"{name}  [{remarks}]" if remarks else name
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, it)
            item.setToolTip(self._poster_tip(it))
            self.list_results.addItem(item)
            # 海报视图（竖版 2:3，占位先行，下载后替换）
            pit = QListWidgetItem()
            pit.setData(Qt.UserRole, it)
            pit.setToolTip(self._poster_tip(it))
            pit.setTextAlignment(Qt.AlignHCenter | Qt.AlignBottom)
            self.list_posters.addItem(pit)
            self._items_by_row.append(it)
            vod_id = str(it.get("vod_id", ""))
            pic = it.get("vod_pic") or ""
            pix = _get_poster(vod_id)
            if pix is not None:
                pit.setIcon(QIcon(pix))
            else:
                pit.setIcon(QIcon(_make_poster_placeholder(name)))
                if pic:
                    poster_jobs.append((vod_id, pic))
        if poster_jobs:
            self._start_poster_jobs(poster_jobs)
        n = len(result)
        if n == 0:
            err = getattr(self._movie, "last_search_error", "") or ""
            self.lbl_status.setText(f"搜索失败: {err}" if err else "无结果")
        else:
            src = getattr(self._movie, "last_search_source", None)
            active = self._current_source_name()
            if src and src != active:
                self.lbl_status.setText(f"已从 {src} 找到 {n} 部")
            else:
                self.lbl_status.setText(f"找到 {n} 部")

    @staticmethod
    def _poster_tip(it):
        """悬停 tooltip：片名 / 评分 / 年份 / 备注。"""
        parts = [it.get("vod_name") or ""]
        score = it.get("vod_score") or ""
        if score:
            parts.append(f"评分 {score}")
        if it.get("vod_year"):
            parts.append(str(it["vod_year"]))
        if it.get("vod_remarks"):
            parts.append(str(it["vod_remarks"]))
        return "  ".join(p for p in parts if p)

    def _on_search_failed(self, err: str):
        self.lbl_status.setText(f"搜索失败: {err}")

    # ---- 播放 ----

    def _on_item_activated(self, item):
        # 双击 → 直接播放（先取消单击详情定时器，防双击误触详情页）
        self._click_timer.stop()
        it = item.data(Qt.UserRole)
        if not isinstance(it, dict):
            return
        try:
            if MovieController.is_multi_episode(it):
                self._show_episode_dialog(it)
                return
            self._play_item(it, index=None)
        except Exception as e:
            self.lbl_status.setText(f"播放失败: {e}")

    def _on_item_clicked(self, item):
        """单击 → 300ms 后开详情页（双击先触发单击，用 timer 让位）。"""
        self._pending_click_item = item
        self._click_timer.start()

    def _on_click_timeout(self):
        item = self._pending_click_item
        self._pending_click_item = None
        if item is not None:
            self._show_detail(item)

    def _show_detail(self, item):
        it = item.data(Qt.UserRole)
        if not isinstance(it, dict):
            return
        dlg = _MovieDetailDialog(it, self)
        dlg.exec()

    def _on_toggle_view(self):
        self._poster_mode = not self._poster_mode
        if self._poster_mode:
            self.stack_results.setCurrentWidget(self.list_posters)
            self.btn_view.setText("海报")
            self.btn_view.setChecked(True)
        else:
            self.stack_results.setCurrentWidget(self.list_results)
            self.btn_view.setText("列表")
            self.btn_view.setChecked(False)

    def _current_source_name(self):
        """当前源名（管理器读不到时返回 None，不打扰 UI）。"""
        try:
            return self._movie._source_manager.get_active_name()
        except Exception:
            return None

    def _refresh_source_label(self):
        """状态栏显示当前源名（如"当前源：量子资源"）。"""
        try:
            name = self._current_source_name()
            self.lbl_source.setText(f"当前源：{name}" if name else "")
        except Exception:
            self.lbl_source.setText("")

    def _on_source_manage(self):
        """"源"按钮 → 源管理对话框（增删/测源/设当前；变更后刷新状态栏）。"""
        try:
            mgr = self._movie._source_manager
        except Exception:
            mgr = None
        dlg = MovieSourceDialog(self, manager=mgr,
                                on_sources_changed=self._refresh_source_label)
        dlg.exec()
        self._refresh_source_label()

    def _start_poster_jobs(self, jobs):
        self._poster_thread = QThread()
        self._poster_worker = _PosterBatchWorker(jobs)
        self._poster_worker.moveToThread(self._poster_thread)
        self._poster_thread.started.connect(self._poster_worker.run)
        self._poster_worker.done.connect(self._on_poster_done)
        self._poster_worker.done.connect(self._poster_thread.quit)
        self._poster_thread.start()

    def _on_poster_done(self, vod_id, path):
        if not path:
            return
        pix = QPixmap(path)
        if pix.isNull():
            return
        _posters_cache[vod_id] = pix
        for i in range(self.list_posters.count()):
            it = self.list_posters.item(i)
            data = it.data(Qt.UserRole)
            if isinstance(data, dict) and str(data.get("vod_id", "")) == vod_id:
                it.setIcon(QIcon(pix))
                break

    def _play_item(self, it, index=None):
        """统一播放入口：控制器播放 + 状态刷新 + 自动字幕（配置开时每片一次）。"""
        try:
            ok = self._movie.play(it, index=index)
            if ok:
                name = it.get("vod_name", "") or ""
                self.lbl_now.setText(f"播放中: {name}")
                self.lbl_status.setText("")
                self.slider_pos.setEnabled(True)
                self._maybe_auto_subtitle(it)
            else:
                self.lbl_status.setText("无法播放：无可用地址")
        except Exception as e:
            self.lbl_status.setText(f"播放失败: {e}")

    def _show_episode_dialog(self, it):
        """多集 → 选集对话框（第01集…，点选即播）。"""
        eps = self._movie.episodes(it)
        if len(eps) <= 1:
            self._play_item(it, index=None)
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"选集 - {(it.get('vod_name') or '')[:24]}")
        dlg.resize(320, 420)
        lay = QVBoxLayout(dlg)
        tip = QLabel(f"共 {len(eps)} 集，双击选集播放（连播开时播完自动下一集）")
        tip.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(tip)
        lst = QListWidget()
        for i, (name, _url) in enumerate(eps):
            itm = QListWidgetItem(name or f"第{i + 1:02d}集", lst)
            itm.setData(Qt.UserRole, i)
        lay.addWidget(lst)
        lst.itemDoubleClicked.connect(lambda itm: self._on_episode_picked(itm, it, eps, dlg))
        dlg.exec()

    def _on_episode_picked(self, itm, it, eps, dlg):
        row = itm.data(Qt.UserRole) if itm.data(Qt.UserRole) is not None else 0
        if 0 <= row < len(eps):
            dlg.accept()
            self._play_item(it, index=row)
            self.lbl_now.setText(f"播放中: {it.get('vod_name', '')} - {eps[row][0] or f'第{row+1:02d}集'}")

    def _maybe_auto_subtitle(self, it):
        """配置 movie.auto_subtitle 开时：每片只自动搜一次（不重复，配额 20/分钟）。
        只搜不加载（避免错配字幕），命中后状态栏提示点"字"选择。"""
        try:
            from ...core.config import get_config
            cfg = get_config()
            if not cfg.get("movie.auto_subtitle", False):
                return
            if self._movie.sub_searched(it):
                return
            self._movie.maybe_mark_sub_searched(it)
            kw = it.get("vod_name") or ""
            if not kw:
                return
            self._sub_thread = QThread()
            self._sub_worker = _SubtitleSearchWorker(self._movie, kw)
            self._sub_worker.moveToThread(self._sub_thread)
            self._sub_thread.started.connect(self._sub_worker.run)
            self._sub_worker.done.connect(self._on_auto_sub_done)
            self._sub_worker.done.connect(self._sub_thread.quit)
            self._sub_thread.start()
        except Exception:
            pass

    def _on_auto_sub_done(self, subs: list):
        n = len(subs or [])
        if n > 0:
            self.lbl_status.setText(f"已找到 {n} 个字幕，点「字」加载")

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
        """点倍速 → 弹菜单选档（当前档勾选高亮）；对齐 B站/爱奇艺交互。"""
        menu = QMenu(self)
        for i, rate in enumerate(self._speed_steps):
            label = self._speed_label(rate)
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(i == self._speed_idx)
            act.triggered.connect(
                lambda _checked=False, r=rate, idx=i: self._apply_speed(r, idx))
        menu.exec_(self.btn_speed.mapToGlobal(self.btn_speed.rect().bottomLeft()))

    @staticmethod
    def _speed_label(rate):
        return f"{rate:g}x" if rate != int(rate) else f"{int(rate)}x"

    def _apply_speed(self, rate, idx=None):
        """应用倍速档位（菜单 action 与测试共用入口）。"""
        if idx is None:
            try:
                idx = self._speed_steps.index(rate)
            except ValueError:
                idx = self._speed_idx
        self._speed_idx = idx
        self.btn_speed.setText(f"倍速 {self._speed_label(rate)}")
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

    # ---- 字幕 ----

    def _on_subtitle(self):
        """"字"按钮 → 字幕对话框（搜索框预填当前片名 + 结果列表，双击加载）。"""
        it = self._movie.current_item()
        default_kw = (it or {}).get("vod_name") or ""
        dlg = _SubtitleDialog(self._movie, default_kw, self)
        dlg.exec()

    def _tick(self):
        try:
            # 自动连播检测：先于播放状态刷新（播完一集 idle 上升沿 → 下一集）
            if hasattr(self._movie, "tick"):
                try:
                    self._movie.tick()
                except Exception:
                    pass
            if not self._movie.is_playing():
                self.lbl_res.setText("")
                return
            try:
                res = self._movie.get_resolution()
            except Exception:
                res = ""
            if res and res != self.lbl_res.text():
                self.lbl_res.setText(res)
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


class _SubtitleSearchWorker(QObject):
    """后台搜字幕（Assrt）。done(subs 列表)。"""

    done = Signal(list)

    def __init__(self, movie, keyword):
        super().__init__()
        self._movie = movie
        self._kw = keyword

    def run(self):
        try:
            self.done.emit(self._movie.search_subtitle(self._kw) or [])
        except Exception:
            self.done.emit([])


class _SubtitleLoadWorker(QObject):
    """后台下载 + 解压 + 加载字幕。done(ok, msg)。"""

    done = Signal(bool, str)

    def __init__(self, movie, sub_id):
        super().__init__()
        self._movie = movie
        self._sub_id = sub_id

    def run(self):
        try:
            ok, msg = self._movie.load_subtitle(self._sub_id)
            self.done.emit(ok, msg)
        except Exception as e:
            self.done.emit(False, f"字幕加载失败: {e}")


class _SubtitleDialog(QDialog):
    """字幕对话框：搜索框（预填当前片名）+ 结果列表（native_name / 语言 / 格式 / 评分），
    双击条目 → 后台下载加载 → 状态反馈。失败降级不崩。"""

    def __init__(self, movie, default_kw="", parent=None):
        super().__init__(parent)
        self._movie = movie
        self.setWindowTitle("字幕（Assrt）")
        self.resize(460, 420)

        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.edit_kw = QLineEdit(default_kw)
        self.edit_kw.setPlaceholderText("输入片名搜索字幕")
        self.btn_go = QPushButton("搜索")
        self.btn_go.setProperty("class", "primary")
        row.addWidget(self.edit_kw, 1)
        row.addWidget(self.btn_go)
        lay.addLayout(row)

        self.lbl_state = QLabel("")
        self.lbl_state.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(self.lbl_state)

        self.list_subs = QListWidget()
        lay.addWidget(self.list_subs, 1)

        self._items = []
        self._thread = None
        self.btn_go.clicked.connect(self._do_search)
        self.edit_kw.returnPressed.connect(self._do_search)
        self.list_subs.itemDoubleClicked.connect(self._on_pick)

    def _do_search(self):
        kw = self.edit_kw.text().strip()
        if not kw:
            self.lbl_state.setText("请输入片名")
            return
        if not self._movie.subtitle_token():
            self.lbl_state.setText("未配置字幕 Token（设置 → 音乐 → 字幕 Token）")
            return
        self.list_subs.clear()
        self._items = []
        self.lbl_state.setText("搜索中...")
        self._thread = QThread()
        self._worker = _SubtitleSearchWorker(self._movie, kw)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_search_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _on_search_done(self, subs: list):
        self._items = subs or []
        if not subs:
            self.lbl_state.setText("未找到字幕")
            return
        for s in subs:
            name = s.get("native_name") or "?"
            lang = s.get("lang_desc") or ""
            sub_type = s.get("subtype") or ""
            score = s.get("vote_score") or 0
            text = f"{name}  [{lang} {sub_type}]  评分 {score}"
            itm = QListWidgetItem(text)
            itm.setData(Qt.UserRole, s.get("id"))
            self.list_subs.addItem(itm)
        self.lbl_state.setText(f"{len(subs)} 条结果，双击加载")

    def _on_pick(self, item):
        sub_id = item.data(Qt.UserRole)
        if sub_id is None:
            return
        self.lbl_state.setText("下载中...")
        self._thread = QThread()
        self._worker = _SubtitleLoadWorker(self._movie, sub_id)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_load_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _on_load_done(self, ok: bool, msg: str):
        if ok:
            self.lbl_state.setText(f"字幕已加载: {msg}")
        else:
            self.lbl_state.setText(str(msg) or "字幕加载失败")


class _MovieDetailDialog(QDialog):
    """详情页：大图 + 评分/导演/演员/简介 + 播放按钮（对齐爱奇艺点击进详情）。"""

    def __init__(self, it, panel, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{(it.get('vod_name') or '')[:24]} - 详情")
        self.resize(540, 660)
        self._panel = panel
        self._it = it

        root = QVBoxLayout(self)

        vod_id = str(it.get("vod_id", ""))
        pix = _get_poster(vod_id)
        if pix is None:
            pix = _make_poster_placeholder(it.get("vod_name") or "")
        self.lbl_pic = QLabel()
        self.lbl_pic.setAlignment(Qt.AlignCenter)
        self.lbl_pic.setPixmap(
            pix.scaled(180, 270, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        root.addWidget(self.lbl_pic)

        lines = [it.get("vod_name") or ""]
        meta = " / ".join(x for x in (
            str(it.get("vod_year") or ""),
            str(it.get("vod_remarks") or ""),
            ("评分 " + str(it.get("vod_score"))) if it.get("vod_score") else "",
        ) if x)
        if meta:
            lines.append(meta)
        if it.get("vod_director"):
            lines.append("导演: " + str(it["vod_director"]))
        if it.get("vod_actor"):
            lines.append("演员: " + str(it["vod_actor"]))
        content = _strip_html(it.get("vod_content") or "")
        if content:
            lines.append("")
            lines.append(content)
        self.lbl_info = QLabel("\n".join(lines))
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.lbl_info)
        root.addWidget(scroll, 1)

        self.btn_play = QPushButton("播放")
        self.btn_play.clicked.connect(self._on_play)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.btn_play)
        row.addStretch()
        root.addLayout(row)

    def _on_play(self):
        it = self._it
        self.accept()
        try:
            if MovieController.is_multi_episode(it):
                self._panel._show_episode_dialog(it)
            else:
                self._panel._play_item(it, index=None)
        except Exception as e:
            self._panel.lbl_status.setText(f"播放失败: {e}")
