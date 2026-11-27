"""音乐面板（本地 + 在线）。"""
import logging
from contextlib import ExitStack
import threading
from ...core.notifications import notify, get_notification_center, current_operation_id
import time
import zlib

from PySide6.QtCore import QEvent, QObject, QPoint, QSignalBlocker, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QActionGroup, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.command_bus import CommandType, get_command_bus
from ...core.event_bus import EventType, get_event_bus
from ...core.operation_result import OperationResult, OperationStatus, media_result
from ...music.humming import HummingService
from ...music.lyric_search import LyricIndex
from ...music.online.events import ONLINE_ERROR, ONLINE_SEARCH_DONE
from ...music.online.models import OnlineTrack
from ..dialogs.identify_dialog import IdentifyDialog
from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.current_song_like import CurrentSongLikeButton
from ..widgets.icon_utils import lucide_icon
from ..widgets.latest_task import LatestTask
from ..widgets.media_controls import FilterRow, PLAY_LOADING, PLAY_RETRY, SEARCHING
from ..widgets.music_content_stack import MediaTabs, MusicContentStack, PageTransition
from ..widgets.music_card_delegate import MusicCardDelegate, MusicCardRoles
from ..widgets.music_queue_dialog import MusicQueueDialog
from ..widgets.music_wheel import MusicWheelScroll
from .music_search_order import rank_manual_search

logger = logging.getLogger(__name__)


class _LyricBuildWorker(QObject):
    """后台构建歌词索引（懒加载：首次按歌词搜索 / 扫描完成后重建）。"""
    done = Signal(int)
    failed = Signal(str)

    def __init__(self, index: LyricIndex):
        super().__init__()
        self._index = index

    def run(self):
        try:
            n = self._index.build()
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.done.emit(n)


class _SearchWorker(QObject):
    # object 保留 dict 子类上的本次错误信息；dict 信号会丢失附加状态。
    done = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, music, keyword, generation):
        super().__init__()
        self._music = music
        self._kw = keyword
        self._generation = generation

    @Slot()
    def run(self):
        try:
            out = self._music.search_online(self._kw)
            if not isinstance(out, dict):
                self.failed.emit(self._generation, "音源未返回有效搜索结果，请重试或更换音源")
                return
            self.done.emit(self._generation, out)
        except Exception as e:
            logger.exception("music_search_failed type=%s", type(e).__name__)
            self.failed.emit(self._generation, str(e))


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


class _RankListsWorker(QObject):
    """Read plugin chart groups in the background, preserving partial errors."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, music):
        super().__init__()
        self._music = music

    def run(self):
        try:
            self.done.emit(self._music.get_top_lists())
        except Exception as e:
            self.failed.emit(str(e))


class _RankDetailWorker(QObject):
    """后台拉单榜单歌曲（→ OnlineTrack 列表，复用播放链）。"""
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, music, source, list_id):
        super().__init__()
        self._music = music
        self._source = source
        self._list_id = list_id

    def run(self):
        try:
            r = self._music.get_top_list_detail(self._source, self._list_id)
            if not isinstance(r, dict) or not isinstance(r.get("tracks"), list):
                self.failed.emit("榜单曲目尚未确认，请重试或换一个音源")
                return
            self.done.emit(r)
        except Exception as e:
            self.failed.emit(str(e))


class _TrackTitle(QLabel):
    """Show a clear ellipsis and retain the full title for hover and accessibility."""

    def __init__(self):
        super().__init__()
        self._full_title = ""
        self.setTextFormat(Qt.PlainText)
        self.setProperty("class", "title")
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setMinimumWidth(1)
        self.setMaximumWidth(620)
        self.setText("未播放")

    def setText(self, title):
        if title != self._full_title:
            self._full_title = title
            self.setAccessibleName(title)
            self._elide_title()
            self.updateGeometry()

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(self.fontMetrics().horizontalAdvance(self._full_title) + 4)
        return hint

    def _elide_title(self):
        visible = self.fontMetrics().elidedText(self._full_title, Qt.ElideRight, self.width())
        super().setText(visible)
        self.setToolTip(self._full_title if self._full_title != visible else "")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self._elide_title()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_title()


class MusicPanel(QWidget):
    # P1 候选旁路：后台线程 publish → 此 Signal 桥到主线程渲染
    _search_event = Signal(dict)
    # 识曲语音命令（MUSIC_IDENTIFY）跨线程桥：voice 线程 emit → 主线程弹窗
    _identify_trigger = Signal()
    _status_signal = Signal(str)          # 跨线程状态栏消息桥（换源错误等事件，禁直接 setText）
    _sleep_timer_signal = Signal(bool, int)
    _manual_play_status = Signal(object, object, str)
    _play_result = Signal(bool, str, object)  # 后台播放结果：ok, msg, 目标状态栏（跨线程回主线程）
    _meter_signal = Signal(object, object, float, float)
    _poll_finished = Signal(object)
    _progress_signal = Signal(float, float, object)  # 后台轮询 mpv 进度：pos, dur（跨线程回主线程）
    _volume_signal = Signal(int, int)  # gesture generation, volume; worker -> GUI
    _speed_result = Signal(object, float, int, str)  # 倍速三态结果：True / False / None
    _download_event = Signal(str, int, str, object)
    _queue_changed = Signal()

    def __init__(self, music=None, parent=None):
        super().__init__(parent)
        self._music = music
        self._row_favorites = {}
        self._playlist_task = LatestTask(self)
        self._playlist_task.completed.connect(self._apply_playlists)
        self._volume_request_gen = 0
        self._syncing_volume = False
        self._last_ui_tick = None
        self._queue_dialog = None
        self._queue_retry_count = 0
        self._queue_refresh_timer = QTimer(self)
        self._queue_refresh_timer.setSingleShot(True)
        self._queue_refresh_timer.timeout.connect(self._refresh_queue_view)
        self._queue_changed.connect(self._schedule_queue_refresh, Qt.QueuedConnection)
        self._download_tasks = {}
        self._download_serial = 0
        self._download_pane = None
        self._download_event.connect(self._apply_download_event)
        self._tracks_by_row = []
        self._track_versions = []
        self._retry_online_track = None
        self._local_songs = []
        self._thread = None
        self._scan_thread = None
        self._voice_generation = None   # P1 候选旁路：当前 UI 展示的语音候选代际
        self._identify_auto_generation = 0
        self._search_generation = 0
        self._search_pages = None
        self._search_visible_limit = 30
        self._search_has_buffer = False
        self._active_search = None
        self._pending_search = None
        self._pending_identify_close = None
        self._manual_request = None
        self._manual_play_status.connect(self._on_manual_play_status)
        self._play_result.connect(self._on_play_result)
        self._speed_result.connect(self._after_speed)
        self._playing_card_key = None   # 当前播放卡片 key（由 _refresh 同步到各 Delegate）
        # 榜单 tab 状态
        self._rank_groups = {}          # 插件名 -> [分组{title, lists}]
        self._rank_tracks = []          # 当前榜单歌曲（OnlineTrack 列表）
        # 榜单 QThread 生命周期：线程对象只持一个引用，运行中绝不覆盖
        #（否则旧运行中线程被 GC 销毁 → "QThread: Destroyed while thread is still running"）。
        self._rank_thread = None
        self._rank_detail_thread = None
        # 最新请求补偿：线程运行中来了新请求 → 记待处理（覆盖旧），旧线程 done/failed 后自动补加载
        self._pending_rank_lists = False
        self._pending_rank_detail = None   # {source, list_id, title} 或 None
        self._rank_detail_serial = 0
        self._rank_active_serial = -1
        # 桌面歌词浮窗（首次点开关才创建；显式隐藏不销毁）
        self._lyrics_window = None
        # 哼唱识曲：语音命令跨线程桥 + 复用弹窗单例 + 服务（首次点按钮才建）
        self._identify_trigger.connect(self._on_identify_click)
        get_command_bus().register(CommandType.MUSIC_IDENTIFY, self._on_identify_cmd)
        self._identify_dialog = None
        self._identify_service = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 4)
        root.setSpacing(7)
        self.header = QWidget()
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel("发现音乐")
        heading_font = self.heading.font()
        heading_font.setPointSize(18)
        heading_font.setBold(True)
        self.heading.setFont(heading_font)
        self.heading.setStyleSheet("font-size:23px; font-weight:650;")
        self.header_layout.addWidget(self.heading)
        self.header_layout.addStretch()
        root.addWidget(self.header)
        self.header.hide()
        self.column_headers = []
        self.btn_queue = QPushButton("播放队列")
        self.btn_queue.setToolTip("查看当前歌曲、下一首和待播歌曲")
        self.btn_queue.clicked.connect(self._show_queue)

        self.btn_lyrics = QPushButton("桌面歌词")
        self.btn_lyrics.setCheckable(True)
        self.btn_lyrics.clicked.connect(self._on_toggle_lyrics)
        self.btn_lyrics_adjust = QPushButton("调整")
        self.btn_lyrics_adjust.setAccessibleName("歌词调整")
        self.btn_lyrics_adjust.setToolTip("字号、锁定、显示行数与歌词时间校正")
        self.lyrics_menu = QMenu(self.btn_lyrics_adjust)
        self.lyrics_menu.aboutToShow.connect(self._build_lyrics_menu)
        self.btn_lyrics_adjust.setMenu(self.lyrics_menu)

        self.btn_identify = QPushButton("识曲")
        self.btn_identify.setIcon(lucide_icon("mic", 16, Tokens.TEXT_SECONDARY))
        self.btn_identify.clicked.connect(self._on_identify_click)
        self.lbl_identify_status = QLabel("")
        self.lbl_identify_status.setWordWrap(True)
        self.lbl_identify_status.setTextFormat(Qt.PlainText)
        self.lbl_identify_status.hide()

        self.btn_view_toggle = QPushButton()
        self.btn_view_toggle.setCheckable(True)
        self.btn_view_toggle.setChecked(True)
        self.btn_view_toggle.setIcon(lucide_icon("grid", 16, Tokens.TEXT_PRIMARY))
        self.btn_view_toggle.setAccessibleName("切换网格与列表")
        self.btn_view_toggle.setToolTip("切换歌曲网格与列表")
        self.btn_view_toggle.clicked.connect(self._toggle_view_mode)
        tabs = self.tabs = MediaTabs(("本地", "发现音乐", "歌单"))
        tabs.setCornerWidget(self.btn_view_toggle, Qt.TopRightCorner)
        tabs.currentChanged.connect(self._on_tab_changed)

        self._music_content = QSplitter(Qt.Horizontal)
        self._music_content.setChildrenCollapsible(False)
        self._music_content.addWidget(tabs)
        self._drawer_transition = PageTransition(self)
        self._music_content.splitterMoved.connect(lambda *_: self._drawer_transition.finish())
        self._media_pane = QStackedWidget()
        self._media_pane.setMinimumWidth(275)
        self._drawer_focus = None
        escape_drawer = QShortcut(QKeySequence(Qt.Key_Escape), self._media_pane)
        escape_drawer.setContext(Qt.WidgetWithChildrenShortcut)
        escape_drawer.activated.connect(self._close_media_pane)
        self._content_pages = MusicContentStack()
        self._content_pages.addWidget(self._music_content)
        from ..widgets.playing_lyrics import PlayingLyrics
        self.playing_lyrics = PlayingLyrics(self._content_pages)
        tabs.reduce_motion = self.playing_lyrics.navigation_motion_reduced
        self.playing_lyrics.style_chosen.connect(lambda key: self._change_lyrics_prefs({"stage_style": key}))
        self.playing_lyrics.effect_chosen.connect(lambda key: self._change_lyrics_prefs({"stage_effect": key}))
        self._meter_request = None
        self._meter_wake = threading.Event()
        self._cover_anchor = None
        self.playing_lyrics.stage.meter_requested.connect(self._set_meter_request)
        self._content_pages.entrance_progress.connect(self.playing_lyrics.stage.set_entrance_progress)
        self._meter_signal.connect(self._on_meter_sample, Qt.QueuedConnection)
        self._poll_finished.connect(self._progress_finished, Qt.QueuedConnection)
        self.playing_lyrics.tools.insertWidget(0, self.btn_lyrics)
        self.playing_lyrics.tools.insertWidget(1, self.btn_lyrics_adjust)
        self.playing_lyrics.close_requested.connect(lambda: self._toggle_playing_lyrics(False))
        self._queue_changed.connect(self.playing_lyrics.sync_transport, Qt.QueuedConnection)
        self._content_pages.addWidget(self.playing_lyrics)
        self._media_pane.setParent(self._content_pages)
        self._media_pane.setObjectName("musicSidePanel")
        self._media_pane.setStyleSheet(f"QStackedWidget#musicSidePanel {{ background:{Tokens.BG_ELEVATED}; border:1px solid {Tokens.BORDER}; border-left:2px solid {Tokens.TEXT_SECONDARY}; border-radius:10px; }}")
        self._music_content.setStretchFactor(0, 1)
        self._media_pane.hide()
        root.addWidget(self._content_pages, 1)

        # ---- 本地 Tab ----
        local = QWidget()
        ll = QVBoxLayout(local)

        # 扫描行（顶部）
        scan_row = QHBoxLayout()
        self.btn_scan_local = QPushButton("扫描本地音乐")
        self.btn_scan_local.clicked.connect(self._on_scan_local)
        scan_row.addWidget(self.btn_scan_local)
        self.lbl_scan_status = QLabel("")
        self.lbl_scan_status.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        scan_row.addWidget(self.lbl_scan_status)
        scan_row.addStretch()
        ll.addLayout(scan_row)

        # 搜索行
        local_search_row = QHBoxLayout()
        self.edit_local = QLineEdit()
        self.edit_local.setPlaceholderText("搜索本地歌曲")
        self.cb_local_mode = QComboBox()
        self.cb_local_mode.addItems(["按歌名", "按歌词"])
        self.cb_local_mode.setToolTip("按歌名：匹配标题/歌手；按歌词：模糊匹配本地 .lrc 歌词内容")
        self.btn_local_search = QPushButton("搜索")
        self.btn_local_search.setProperty("class", "primary")
        self.btn_local_clear = QPushButton("清空")
        local_search_row.addWidget(self.edit_local)
        local_search_row.addWidget(self.cb_local_mode)
        local_search_row.addWidget(self.btn_local_search)
        local_search_row.addWidget(self.btn_local_clear)
        ll.addLayout(local_search_row)

        # 本地列表（卡片网格）
        self.list_local = QListWidget()
        self._dl_local = self._setup_card_view(self.list_local)
        self._track_header(ll, self.list_local)
        ll.addWidget(self.list_local, 1)
        # 当前曲目（小标题级）
        self.lbl_track = _TrackTitle()
        from ..widgets.music_chrome import Artwork
        from ..widgets.playback_quality_button import PlaybackQualityButton
        self.cover_thumb = Artwork()
        self.cover_thumb.setFixedSize(48, 48)
        self.quality_button = PlaybackQualityButton(music, self)
        self.lbl_artist = _TrackTitle()
        self.lbl_artist.setText("")
        self.lbl_artist.setProperty("class","secondary")
        self.lbl_track.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        self.lbl_artist.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        self.lbl_artist.setMaximumWidth(150)
        self.lbl_track.setStyleSheet("font-size:15px;font-weight:600;")
        self.lbl_artist.setStyleSheet(f"font-size:12px;color:{Tokens.TEXT_SECONDARY};")
        self.btn_like = CurrentSongLikeButton(music, self)
        self.btn_like.changed.connect(self._refresh_visible_playlists)
        self._queue_changed.connect(self.btn_like.sync, Qt.QueuedConnection)
        self.btn_playing_lyrics = QPushButton("歌词")
        self.btn_playing_lyrics.setCheckable(True)
        self.btn_playing_lyrics.clicked.connect(self._toggle_playing_lyrics)

        # 进度条（律动波形：播放中频谱跳动，暂停低电平）
        from ..widgets.waveform_slider import WaveformSlider
        self.slider_pos = WaveformSlider()
        self.slider_pos.setRange(0, 1000)
        self.slider_pos.setEnabled(False)
        self._wave_slider = self.slider_pos
        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setAlignment(Qt.AlignCenter)
        # 播放时间用 tabular figures（数字等宽，计时变化不跳动；impeccable §craft floor）
        try:
            f = self.lbl_time.font()
            f.setFeature(QFont.Tag("tnum"), 1)
            self.lbl_time.setFont(f)
        except Exception:
            pass  # 旧 Qt 无 QFont.Tag：忽略

        # 模式、播放键、更多选项与音量共用一行。
        row_main = QHBoxLayout()
        self.btn_prev = QPushButton()
        self.btn_prev.setObjectName("btn_prev")
        self.btn_prev.setIcon(lucide_icon("skip-back", 16, Tokens.TEXT_PRIMARY))
        self.btn_prev.setToolTip("上一首")
        self.btn_toggle = QPushButton()
        self.btn_toggle.setObjectName("btn_toggle")
        self.btn_toggle.setIcon(lucide_icon("play", 18, "#ffffff"))
        self.btn_toggle.setToolTip("播放/暂停")
        self.btn_next = QPushButton()
        self.btn_next.setObjectName("btn_next")
        self.btn_next.setIcon(lucide_icon("skip-forward", 16, Tokens.TEXT_PRIMARY))
        self.btn_next.setToolTip("下一首")
        self.btn_mode = QPushButton("顺序")
        self.btn_mode.setObjectName("btn_mode")
        self.btn_mode.setAccessibleName("播放模式")
        self.btn_mode.setIcon(lucide_icon("repeat", 14, Tokens.TEXT_SECONDARY))
        self._mode_menu = QMenu(self.btn_mode)
        self._mode_actions = QActionGroup(self._mode_menu)
        for mode, label in (("sequential", "顺序"), ("shuffle", "随机"),
                            ("repeat_all", "列表循环"), ("repeat_one", "单曲循环")):
            action = self._mode_menu.addAction(label)
            action.setData(mode)
            action.setCheckable(True)
            self._mode_actions.addAction(action)
        self._mode_actions.triggered.connect(self._select_play_mode)
        self._mode_menu.aboutToShow.connect(self._sync_play_mode)
        self.btn_mode.setMenu(self._mode_menu)
        self._set_play_mode_display("sequential")

        self.btn_more = QPushButton("更多")
        self._more_menu = QMenu(self.btn_more)
        self.btn_more.setMenu(self._more_menu)
        self.action_download_tasks = self._more_menu.addAction("下载任务", self._show_download_tasks)
        self.action_desktop_lyrics = self._more_menu.addAction("桌面歌词")
        self.action_desktop_lyrics.setCheckable(True)
        self.action_desktop_lyrics.triggered.connect(self.btn_lyrics.click)
        self.btn_lyrics.toggled.connect(self.action_desktop_lyrics.setChecked)
        self._more_menu.addSeparator()
        self._speed_menu = self._more_menu.addMenu("倍速")
        self._speed_steps = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
        self._setup_speed_actions()

        self.action_highlight = self._more_menu.addAction("高潮模式：关")
        self.action_highlight.setCheckable(True)
        self.action_highlight.triggered.connect(self._on_highlight)
        self._more_menu.addAction("标记高潮起点", self._on_mark_highlight)
        self._sleep_menu = self._more_menu.addMenu("睡眠定时")
        for minutes in (30, 60, 90):
            action = self._sleep_menu.addAction(f"{minutes} 分钟")
            action.triggered.connect(lambda _checked=False, value=minutes:
                                     self._set_sleep_timer(value * 60))
        self._sleep_menu.addAction("取消定时", self._cancel_sleep_timer)
        self._more_menu.addSeparator()
        self._more_menu.addAction("清空队列…", self._on_clear_queue)
        self._more_menu.aboutToShow.connect(self._sync_highlight)
        self._more_menu.aboutToShow.connect(self._sync_speed)
        self._speed_menu.aboutToShow.connect(self._sync_speed)
        self.btn_mode.setEnabled(music is not None)
        self.btn_more.setEnabled(music is not None)
        # 音量：MusicFree 悬浮极简风——平时仅喇叭图标，悬停/点击弹出垂直玻璃面板
        from ..widgets.glass_slider import GlassSlider, VolumeButton
        self.btn_volume = VolumeButton()
        self.slider_vol = GlassSlider(Qt.Vertical)
        self.slider_vol.setRange(0, 100)
        self.btn_volume.set_slider(self.slider_vol)
        footer = QFrame()
        footer.setObjectName("musicTransport")
        footer.setStyleSheet(f"QFrame#musicTransport {{ border-top: 1px solid {Tokens.BORDER}; }}")
        outer = QGridLayout(footer)
        outer.setContentsMargins(8, 6, 8, 5)
        outer.setHorizontalSpacing(14)
        identity = QHBoxLayout()
        identity.setSpacing(9)
        identity.addWidget(self.cover_thumb)
        names = QVBoxLayout()
        names.setSpacing(1)
        self.lbl_track.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_artist.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        names.addWidget(self.lbl_track)
        names.addWidget(self.lbl_artist)
        names.addWidget(self.quality_button, 0, Qt.AlignLeft)
        identity.addLayout(names, 1)
        outer.addLayout(identity, 0, 0, 2, 1)
        outer.setVerticalSpacing(1)
        row_main.setSpacing(5)
        row_main.addStretch()
        for control in (self.btn_mode, self.btn_prev, self.btn_toggle, self.btn_next):
            row_main.addWidget(control, 0, Qt.AlignCenter)
        row_main.addStretch()
        self.btn_toggle.setFixedSize(42, 42)
        self.btn_toggle.setAccessibleName("播放或暂停")
        self.btn_toggle.setStyleSheet(f"QPushButton {{ background:{Tokens.ACCENT}; border:0; border-radius:21px; }}")
        outer.addLayout(row_main, 0, 1)
        timeline = QHBoxLayout()
        self.slider_pos.setFixedHeight(20)
        timeline.addWidget(self.slider_pos, 1)
        timeline.addWidget(self.lbl_time)
        outer.addLayout(timeline, 1, 1)
        auxiliary = QHBoxLayout()
        auxiliary.setSpacing(2)
        auxiliary.addStretch()
        for control in (self.btn_like, self.btn_playing_lyrics, self.btn_queue, self.btn_more, self.btn_volume):
            auxiliary.addWidget(control, 0, Qt.AlignVCenter)
        self.btn_queue.setText("队列")
        outer.addLayout(auxiliary, 0, 2)
        outer.setColumnStretch(0, 4)
        outer.setColumnStretch(1, 6)
        outer.setColumnStretch(2, 3)
        root.addWidget(footer)

        # 键盘优先：Ctrl+F / Ctrl+K 聚焦搜索框（窗口级快捷键）
        self._sc_find = QShortcut(
            QKeySequence("Ctrl+F"), self, self._focus_search, Qt.WindowShortcut)
        self._sc_k = QShortcut(
            QKeySequence("Ctrl+K"), self, self._focus_search, Qt.WindowShortcut)

        tabs.addTab(local, "本地")

        # ---- 在线 Tab ----
        online = QWidget()
        ol = QVBoxLayout(online)
        search_row = QHBoxLayout()
        self.edit_kw = QLineEdit()
        self.edit_kw.setPlaceholderText("想听什么？搜索歌曲、歌手")
        self.btn_search = QPushButton("搜索")
        self.btn_search.setProperty("class", "primary")
        self.cb_source = QComboBox()
        self.cb_source.addItem("全部", userData=None)
        self.cb_source.setToolTip("搜索音源范围；不会改变正在播放的歌曲")
        self.cb_search_filter = QComboBox()
        self.cb_search_filter.addItems(["全部版本", "现场标记", "翻唱标记"])
        self.cb_search_filter.setToolTip("只按标题中明确的现场/Live、翻唱/Cover标记筛选，不推测原唱")
        self.cb_search_filter.currentIndexChanged.connect(self._apply_online_filter)
        self.btn_discovery_home = QPushButton("返回发现")
        self.btn_discovery_home.clicked.connect(self._show_discovery_home)
        self.search_options = QPushButton()
        self.search_options.setToolTip("按歌曲版本或来源筛选")
        self.search_filters = FilterRow(self.search_options, self.cb_search_filter, self.cb_source)
        search_row.addWidget(self.edit_kw, 1)
        search_row.addWidget(self.btn_search)
        search_row.addWidget(self.search_options)
        search_row.addWidget(self.btn_identify)
        search_row.addWidget(self.btn_discovery_home)
        ol.addLayout(search_row)
        ol.addWidget(self.search_filters)
        ol.addWidget(self.lbl_identify_status)
        self._results_requested = False
        self._last_online_result = None
        self._discovery_tracks = None
        self._last_online_query = ""
        self.online_stack = QStackedWidget()
        from ..widgets.music_discovery import MusicDiscovery
        self.discovery = MusicDiscovery(music, online)
        self.discovery.navigate.connect(tabs.setCurrentIndex)
        self.discovery.tracks_ready.connect(self._show_discovered_tracks)
        self.discovery.history_chosen.connect(self._play_discovery_history)
        self.discovery.playlist_chosen.connect(self._open_discovery_playlist)
        self.online_stack.addWidget(self.discovery)
        self.results_page = QWidget()
        results_layout = QVBoxLayout(self.results_page)
        results_layout.setContentsMargins(0, 0, 0, 0)
        self.list_results = QListWidget()
        from ..widgets.music_card_delegate import DownloadCardDelegate
        self._dl_results = self._setup_card_view(self.list_results, DownloadCardDelegate)
        self._dl_results.download_requested.connect(self._download_result)
        download_shortcut = QShortcut(QKeySequence("Ctrl+D"), self.list_results)
        download_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        download_shortcut.activated.connect(lambda: self._download_result(self.list_results.currentRow()))
        self._track_header(results_layout, self.list_results, download=True)
        results_layout.addWidget(self.list_results, 1)
        self.lbl_status = QLabel("输入关键词开始搜索")
        self.lbl_status.setTextFormat(Qt.PlainText)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        actions = QHBoxLayout()
        actions.addWidget(self.lbl_status, 1)
        self.btn_result_play = QPushButton("播放")
        self.btn_result_play.setProperty("class", "primary")
        self.btn_result_play.clicked.connect(lambda: self._on_play(self.list_results.currentItem())
                                            if self.list_results.currentItem() else None)
        self.btn_versions = QPushButton("换一个版本")
        self.btn_versions.clicked.connect(self._choose_online_version)
        self.btn_retry_online = QPushButton("重试")
        self.btn_retry_online.clicked.connect(self._retry_online)
        self.btn_retry_online.hide()
        self.btn_search_more = QPushButton("加载更多")
        self.btn_search_more.clicked.connect(self._load_search_more)
        self.btn_search_more.hide()
        self.btn_search_retry = QPushButton("重试未完成来源")
        self.btn_search_retry.clicked.connect(self._retry_search_pages)
        self.btn_search_retry.hide()
        self._search_page_timer = QTimer(self)
        self._search_page_timer.setInterval(25)
        self._search_page_timer.timeout.connect(self._poll_search_pages)
        self.edit_kw.textChanged.connect(self._search_keyword_changed)
        self.btn_result_play.setEnabled(False)
        self.btn_result_play.hide()
        self.btn_versions.setEnabled(False)
        self.btn_versions.hide()
        self.list_results.currentRowChanged.connect(self._online_selection_changed)
        for button in (self.btn_result_play, self.btn_versions, self.btn_retry_online,
                       self.btn_search_more, self.btn_search_retry):
            actions.addWidget(button)
        results_layout.addLayout(actions)
        self.btn_discovery_more = QPushButton("加载更多曲目")
        self.btn_discovery_more.hide()
        self.btn_discovery_more.clicked.connect(self.discovery._more)
        self.discovery.paging_changed.connect(self.btn_discovery_more.setVisible)
        results_layout.addWidget(self.btn_discovery_more)
        results_layout.addStretch(0)
        self._results_fit = QTimer(self)
        self._results_fit.setSingleShot(True)
        self._results_fit.timeout.connect(self._fit_sparse_results)
        model = self.list_results.model()
        for changed in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            changed.connect(lambda *_: self._results_fit.start(0))
        self._fit_sparse_results()
        self.online_stack.addWidget(self.results_page)
        self.btn_discovery_home.hide()
        self.online_stack.currentChanged.connect(
            lambda _: self.btn_discovery_home.setVisible(self.online_stack.currentWidget() is self.results_page))
        ol.addWidget(self.online_stack, 1)
        tabs.addTab(online, "发现音乐")

        # ---- 历史 Tab ----
        history = QWidget()
        hl2 = QVBoxLayout(history)
        hrow2 = QHBoxLayout()
        self.btn_clear_history = QPushButton("清空历史")
        hrow2.addWidget(self.btn_clear_history)
        hrow2.addStretch()
        hl2.addLayout(hrow2)
        self.list_history = QListWidget()
        self._dl_history = self._setup_card_view(self.list_history)
        self._track_header(hl2, self.list_history)
        hl2.addWidget(self.list_history)
        tabs.addTab(history, "历史")

        # ---- 歌单 Tab ----
        playlist = QWidget()
        pl = QHBoxLayout(playlist)
        pl_left = QVBoxLayout()
        self.list_playlists = QListWidget()
        pl_left.addWidget(self.list_playlists, 2)
        pl_btns = QHBoxLayout()
        self.btn_new_playlist = QPushButton("新建歌单")
        self.btn_rename_playlist = QPushButton("重命名")
        self.btn_del_playlist = QPushButton("删除歌单")
        pl_btns.addWidget(self.btn_new_playlist)
        pl_btns.addWidget(self.btn_rename_playlist)
        pl_btns.addWidget(self.btn_del_playlist)
        pl_left.addLayout(pl_btns)
        self._favorites_only = False
        self._browse_playlist_id = None
        collection = self.playlist_collection = QWidget()
        collection.setMinimumWidth(140)
        collection.setMaximumWidth(210)
        collection.setLayout(pl_left)
        pl.addWidget(collection)
        pl_right = QVBoxLayout()
        self.list_playlist_songs = QListWidget()
        self._dl_playlist_songs = self._setup_card_view(self.list_playlist_songs)
        from ..widgets.music_chrome import Artwork, PlaylistHeader
        self.playlist_header = PlaylistHeader()
        detail = QHBoxLayout(self.playlist_header)
        detail.setContentsMargins(10, 8, 12, 8)
        detail.setSpacing(12)
        self.playlist_art = Artwork()
        self.playlist_art.setFixedSize(60, 60)
        detail.addWidget(self.playlist_art)
        summary = QVBoxLayout()
        summary.setSpacing(2)
        self.playlist_title = _TrackTitle()
        self.playlist_title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.playlist_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.playlist_title.setStyleSheet("font-size:20px;font-weight:600;")
        self.playlist_title.setText("歌单")
        summary.addWidget(self.playlist_title)
        self.lbl_playlist_status = QLabel("未选择歌单")
        self.lbl_playlist_status.setProperty("class", "secondary")
        summary.addWidget(self.lbl_playlist_status)
        detail.addLayout(summary, 1)
        self.play_playlist_button = QPushButton("播放")
        self.play_playlist_button.setProperty("class", "primary")
        self.play_playlist_button.hide()
        self.play_playlist_button.clicked.connect(lambda: self._on_playlist_song_play(self.list_playlist_songs.item(0))
                                                if self.list_playlist_songs.count() else None)
        detail.addWidget(self.play_playlist_button)
        pl_right.addWidget(self.playlist_header)
        self._track_header(pl_right, self.list_playlist_songs)
        pl_right.addWidget(self.list_playlist_songs, 1)
        pl.addLayout(pl_right, 3)
        tabs.addTab(playlist, "歌单")

        # ---- 榜单 Tab（左：平台 + 榜单列表；右：榜单歌曲）----
        rank = QWidget()
        from ..widgets.popular_songs import PopularSongs
        rank_layout = QVBoxLayout(rank)
        self.rank_mode = QPushButton("按来源查看")
        self.rank_mode.setCheckable(True)
        rank_layout.addWidget(self.rank_mode, 0, Qt.AlignRight)
        self.rank_pages = QStackedWidget()
        rank_layout.addWidget(self.rank_pages, 1)
        self.popular_songs = PopularSongs(music, rank)
        self.popular_songs.play_requested.connect(self._play_popular_song)
        self.rank_pages.addWidget(self.popular_songs)
        source_page = QWidget()
        self.rank_pages.addWidget(source_page)
        self.rank_mode.toggled.connect(self._rank_mode_changed)
        rl = QHBoxLayout(source_page)
        rl_left = QVBoxLayout()
        lbl_src = QLabel("平台")
        lbl_src.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        rl_left.addWidget(lbl_src)
        self.list_rank_sources = QListWidget()
        rl_left.addWidget(self.list_rank_sources, 1)
        lbl_lst = QLabel("榜单")
        lbl_lst.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        rl_left.addWidget(lbl_lst)
        self.list_rank_lists = QListWidget()
        rl_left.addWidget(self.list_rank_lists, 1)
        rl.addLayout(rl_left, 2)
        rl_right = QVBoxLayout()
        self.list_rank_songs = QListWidget()
        self._dl_rank_songs = self._setup_card_view(self.list_rank_songs)
        self._track_header(rl_right, self.list_rank_songs)
        rl_right.addWidget(self.list_rank_songs, 1)
        self.lbl_rank_status = QLabel("加载榜单中…")
        self.lbl_rank_status.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        rl_right.addWidget(self.lbl_rank_status)
        rl.addLayout(rl_right, 3)
        tabs.addTab(rank, "榜单")
        tabs.setCurrentWidget(rank)

        # 播放按钮图标锁定（防 mpv 异步启动期间图标闪回）
        self._icon_locked = False
        self._poll_ticks = 0
        # 绑定
        if music is not None:
            self.btn_prev.clicked.connect(self._on_prev)
            self.btn_toggle.clicked.connect(self._on_toggle)
            self.btn_next.clicked.connect(self._on_next)
            self.slider_vol.valueChanged.connect(self._on_volume)
            self.slider_vol.sliderReleased.connect(self._on_volume_released)
            self.slider_pos.sliderReleased.connect(self._on_seek)
            self.slider_pos.chorusClicked.connect(self._on_chorus_click)
            # 拖动过程实时预览目标时间（仅内存级 UI 更新，不碰 mpv IPC）
            self.slider_pos.sliderMoved.connect(self._on_seek_preview)
            self.btn_search.clicked.connect(self._request_search)
            self.edit_kw.returnPressed.connect(self._request_search)
            self.list_results.itemDoubleClicked.connect(self._on_play)
            self.list_history.itemDoubleClicked.connect(self._on_history_play)
            self.btn_clear_history.clicked.connect(self._on_clear_history)
            self.btn_local_search.clicked.connect(self._do_local_search)
            self.edit_local.returnPressed.connect(self._do_local_search)
            self.btn_local_clear.clicked.connect(self._load_local_all)
            self.list_local.itemDoubleClicked.connect(self._on_local_play)
            # 歌单 tab
            self.list_playlists.currentItemChanged.connect(self._on_playlist_selected)
            self.btn_new_playlist.clicked.connect(self._on_new_playlist)
            self.btn_rename_playlist.clicked.connect(self._on_rename_playlist)
            self.btn_del_playlist.clicked.connect(self._on_delete_playlist)
            self.list_playlist_songs.itemDoubleClicked.connect(self._on_playlist_song_play)
            # 榜单 tab
            self.list_rank_sources.currentItemChanged.connect(self._on_rank_source_selected)
            self.list_rank_lists.currentItemChanged.connect(self._on_rank_list_selected)
            self.list_rank_songs.itemDoubleClicked.connect(self._on_rank_song_play)
            # 右键菜单（QMenu parent=self 防 GC）
            self.list_local.setContextMenuPolicy(Qt.CustomContextMenu)
            self.list_local.customContextMenuRequested.connect(self._on_local_context_menu)
            self.list_history.setContextMenuPolicy(Qt.CustomContextMenu)
            self.list_history.customContextMenuRequested.connect(self._on_history_context_menu)
            self.list_playlist_songs.setContextMenuPolicy(Qt.CustomContextMenu)
            self.list_playlist_songs.customContextMenuRequested.connect(self._on_playlist_songs_context_menu)
            self.list_results.setContextMenuPolicy(Qt.CustomContextMenu)
            self.list_results.customContextMenuRequested.connect(self._on_online_context_menu)
            try:
                self._syncing_volume = True
                initial_volume = getattr(music, "default_volume", 70)
                self.slider_vol.setValue(initial_volume if type(initial_volume) is int else 70)
            except Exception:
                pass
            finally:
                self._syncing_volume = False
            try:
                for name in (music.get_online_sources() or []):
                    self.cb_source.addItem(name, userData=name)
            except Exception:
                pass
            # 初始加载全部
            self._load_local_all()
            self._refresh_history()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)
        self._music_wheel = MusicWheelScroll(self)
        for area in self.tabs.findChildren(QAbstractScrollArea):
            self._music_wheel.install(area)

        # 进度轮询后台线程：mpv IPC（playback-time/duration）绝不在主线程同步读取
        self._chorus_cancel = threading.Event()
        self._progress_polling = False
        self._last_dur = 0.0  # 最近一次进度快照的时长，供拖动预览换算（_on_seek_preview）
        self._progress_signal.connect(self._on_progress)
        self._volume_signal.connect(self._on_volume_snapshot, Qt.QueuedConnection)

        # P1 候选旁路：订阅 controller 推送的候选事件（Signal 桥到主线程）
        self._search_event.connect(self._on_search_show)
        self._status_signal.connect(self._on_status_msg)
        self._sleep_timer_signal.connect(self._on_sleep_timer_state)
        subscriptions = self._event_subscriptions = ExitStack()
        self.destroyed.connect(subscriptions.close)
        try:
            bus = get_event_bus()
            for event_type, handler in (
                (ONLINE_SEARCH_DONE, self._on_search_event),
                (ONLINE_ERROR, self._on_error_event),
                (EventType.MUSIC_STATE_CHANGED, self._on_music_state_event),
            ):
                bus.subscribe(event_type, handler)
                subscriptions.callback(bus.unsubscribe, event_type, handler)
        except Exception as e:
            print(f"[MusicPanel] 订阅候选事件失败: {e}")

    def closeEvent(self, event):
        """关闭时注销事件总线订阅（防内存泄漏/幽灵回调）：
        与 __init__ 中 subscribe 的一一配对注销。"""
        self._cancel_manual_feedback()
        self._queue_refresh_timer.stop()
        self._stop_search_pages()
        if hasattr(self, "btn_like"):
            self.btn_like.shutdown()
            self.quality_button.shutdown()
        for task in self._row_favorites.values():
            task.cancel()
        self._row_favorites.clear()
        self._playlist_task.cancel()
        if hasattr(self, "popular_songs"):
            self.popular_songs.shutdown()
        if hasattr(self, "discovery"):
            self.discovery.shutdown()
        if self._queue_dialog is not None:
            self._queue_dialog.close()
        self._close_media_pane(animate=False)
        self._toggle_playing_lyrics(False, animate=False)
        if self._lyrics_window is not None:
            self._lyrics_window.close()
            from .settings_panel import unregister_lyrics_prefs_listener
            unregister_lyrics_prefs_listener(self._lyrics_window.apply_prefs)
        self._event_subscriptions.close()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._last_ui_tick = None
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.start()
        if hasattr(self, "btn_like"):
            self.btn_like.sync()
        self._chorus_cancel = threading.Event()
        self._progress_polling = True
        if self.btn_playing_lyrics.isChecked() and self._lyrics_window is not None:
            self._lyrics_window.start()
        self._start_progress_poll()

    def resizeEvent(self, event):
        self._drawer_transition.finish()
        super().resizeEvent(event)
        if hasattr(self, "_media_pane"):
            self._position_media_pane()

    def hideEvent(self, event):
        self._drawer_transition.finish()
        self._cancel_manual_feedback()
        super().hideEvent(event)
        # A Qt popup can stay visible after its owning panel has been hidden.
        if hasattr(self, "_mode_menu"):
            self._mode_menu.close()
            self._more_menu.close()
        self._last_ui_tick = None
        self._queue_refresh_timer.stop()
        if self._queue_dialog is not None and self._media_pane.currentWidget() is self._queue_dialog:
            self._close_media_pane(animate=False)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.stop()
        self._progress_polling = False
        self._set_meter_request(None)
        self._meter_wake.set()
        if self._lyrics_window is not None and not self.btn_lyrics.isChecked():
            self._lyrics_window.stop()
        if hasattr(self, "_chorus_cancel"):
            self._chorus_cancel.set()
            self.slider_pos.set_chorus(None)

    @Slot()
    def _show_queue(self):
        if self._queue_dialog is None:
            self._queue_dialog = MusicQueueDialog(self._media_pane, embedded=True)
            self._media_pane.addWidget(self._queue_dialog)
            self._queue_dialog.close_requested.connect(self._close_media_pane)
            self._queue_dialog.clear_requested.connect(self._on_clear_queue)
            self._queue_dialog.next_requested.connect(self._queue_entry_next)
            self._queue_dialog.finished.connect(self._queue_refresh_timer.stop)
            self._queue_dialog.remove_requested.connect(self._remove_queue_item, Qt.QueuedConnection)
            self._queue_dialog.move_requested.connect(self._move_queue_item, Qt.QueuedConnection)
        if (self._media_pane.isVisible() and self._media_pane.currentWidget() is self._queue_dialog
                and self._queue_dialog.isVisible()):
            self._close_media_pane()
            return
        self._open_media_pane(self._queue_dialog)
        self._schedule_queue_refresh()

    @Slot()
    def _schedule_queue_refresh(self):
        # EventBus callbacks may run on worker threads; only this queued slot touches Qt.
        if self._queue_dialog is not None and self._queue_dialog.isVisible():
            self._queue_retry_count = 0
            if not self._queue_refresh_timer.isActive():
                self._queue_refresh_timer.start(0)

    @Slot()
    def _refresh_queue_view(self):
        if self._queue_dialog is None or not self._queue_dialog.isVisible():
            return
        query = getattr(self._music, "get_queue_snapshot", None)
        try:
            snapshot = query() if callable(query) else None
        except Exception:
            logger.exception("播放队列读取失败")
            self._queue_dialog.show_pending(exhausted=True)
            return
        if snapshot is None:
            # Never wait on playback IPC in the GUI. Retry briefly, only while open.
            self._queue_retry_count += 1
            exhausted = self._queue_retry_count >= 6 or not callable(query)
            self._queue_dialog.show_pending(exhausted=exhausted)
            if not exhausted:
                self._queue_refresh_timer.start(200)
            return
        self._queue_dialog.set_snapshot(snapshot)

    @Slot(int, int)
    def _remove_queue_item(self, index, revision):
        try:
            result = self._music.remove_from_queue(index, revision)
        except Exception:
            logger.exception("移出播放队列失败")
            result = False
        if not isinstance(result, OperationResult):
            result = media_result(result, label="歌曲", action="移出播放队列")
        self._queue_dialog.action_status.setText(result.message)
        notify("已移出播放队列" if result.status is OperationStatus.CONFIRMED else "歌曲移出结果",
               result.message, status="success" if result.status is OperationStatus.CONFIRMED else
               "failed" if result.status is OperationStatus.FAILED else "warning", source="music")
        # A stale menu is rejected by the controller; show the latest queue for retry.
        self._schedule_queue_refresh()

    # ---- 卡片网格（只改 UI 绘制，不动数据流） ----
    def _setup_card_view(self, w: QListWidget, delegate=MusicCardDelegate) -> MusicCardDelegate:
        """歌曲类列表 → IconMode 卡片网格 + 自绘 Delegate。"""
        w.setViewMode(QListWidget.IconMode)
        # Sparse data reads as individual tracks rather than a large empty card.
        w.setStyleSheet("QListWidget { background: transparent; border: none; padding: 0; }")
        w.setMovement(QListWidget.Static)
        w.setResizeMode(QListWidget.Adjust)
        w.setUniformItemSizes(True)
        w.setSpacing(Tokens.SPACING_MD)
        w.setMouseTracking(True)
        d = delegate(w)
        w.setItemDelegate(d)
        return d

    @Slot(str, object, int)
    def _move_queue_item(self, entry_id, before_id, revision):
        try:
            result = self._music.move_queue_entry(entry_id, before_id, revision)
        except Exception:
            logger.exception("queue_sort_failed")
            result = False
        if not isinstance(result, OperationResult):
            result = media_result(result, label="队列", action="调整顺序")
        self._queue_dialog.action_status.setText(result.message)
        notify("待播顺序已调整" if result.status is OperationStatus.CONFIRMED else "队列排序未完成",
               result.message, status="success" if result.status is OperationStatus.CONFIRMED else "warning")
        self._schedule_queue_refresh()

    def _fit_sparse_results(self):
        listing = self.list_results
        layout = self.results_page.layout()
        count = listing.count()
        sparse = count <= 2 and listing.viewMode() == QListWidget.ListMode
        height = max(0, listing.sizeHintForRow(0)) * count if sparse else 16777215
        listing.setMaximumHeight(height)
        layout.setStretch(layout.count() - 1, int(sparse))

    def _toggle_view_mode(self, grid_on: bool):
        """列表/网格 全局切换：网格 = 卡片 Delegate；列表 = 默认文本 delegate。"""
        pairs = [
            (self.list_local, self._dl_local),
            (self.list_results, self._dl_results),
            (self.list_history, self._dl_history),
            (self.list_playlist_songs, self._dl_playlist_songs),
            (self.list_rank_songs, self._dl_rank_songs),
        ]
        mode = QListWidget.IconMode if grid_on else QListWidget.ListMode
        for w, d in pairs:
            try:
                w.setViewMode(mode)
                # ListMode 下显式挂默认 delegate（setItemDelegate(None) 会令行高归零）
                w.setProperty("trackTable", not grid_on)
                w.setItemDelegate(d)
                w.setSpacing(Tokens.SPACING_MD if grid_on else 0)
                if w is self.list_results:
                    self._fit_sparse_results()
                if grid_on:
                    w.setMovement(QListWidget.Static)
                    w.setResizeMode(QListWidget.Adjust)
                    w.setSpacing(Tokens.SPACING_MD)
            except RuntimeError:
                pass  # 视图已销毁
        self.popular_songs.set_grid(grid_on)
        for header in self.column_headers:
            header.setVisible(not grid_on)
        self.btn_view_toggle.setIcon(lucide_icon(
            "grid" if grid_on else "list", 16, Tokens.TEXT_PRIMARY))

    @staticmethod
    def _card_data(base: dict, sub: str, play_key) -> dict:
        """卡片 UserRole dict：附第二行文本 + 播放 key（不覆盖原数据）。

        封面字段兜底：不同源字段名不一（cover_url/artwork/coverImg/pic 等），
        统一映射到 cover_url 供 delegate 异步下载；并预计算渐变种子 _seed
        （歌曲名 crc32）供「无封面哈希渐变兜底」直接读取——paint 零计算。
        """
        d = dict(base)
        d["_sub"] = sub
        d["_play_key"] = play_key
        if not d.get("cover_url"):
            extra = d.get("extra")
            raw = extra.get("raw") if isinstance(extra, dict) else None
            for cand in ("cover_url", "cover_img", "artwork", "pic", "cover", "pic_url"):
                v = d.get(cand)                    # base 顶层字段
                if not v and isinstance(extra, dict):
                    v = extra.get(cand)
                if not v and isinstance(raw, dict):
                    v = raw.get(cand) or raw.get("coverUrl")
                if v:
                    d["cover_url"] = v
                    break
        title = d.get("title")
        if title:
            d["_seed"] = zlib.crc32(str(title).encode("utf-8", "ignore"))
        return d

    @staticmethod
    def _play_key(song: dict):
        """当前播放 key（与卡片 item._play_key 对齐；本地按 music_id，在线按 track_id）。"""
        if not song:
            return None
        if song.get("online"):
            tid = song.get("track_id") or ""
            if tid:
                return f"net:{tid}"
            return f"net:{song.get('title')}|{song.get('artist')}"
        sid = song.get("id")
        return f"local:{sid}" if sid else None

    def _track_header(self, layout, listing, *, download=False):
        from ..widgets.music_chrome import TrackColumns
        header = TrackColumns(listing, self._sort_tracks, download)
        self.column_headers.append(header)
        layout.addWidget(header)

    def enable_shell_navigation(self):
        self.tabs.tabBar().hide()
        self.tabs.setCornerWidget(None)
        self.header.show()
        self.edit_kw.setMaximumWidth(440)
        self.header_layout.addWidget(self.edit_kw, 2)
        self.header_layout.addWidget(self.btn_search)
        self.sort_button = QPushButton("排序")
        self.sort_menu = QMenu(self.sort_button)
        for field, label in (("title","歌曲名称"),("artist","歌手"),("album","专辑"),("duration","时长")):
            self.sort_menu.addAction(label, lambda checked=False, key=field: self._sort_tracks(key))
        self.sort_button.setMenu(self.sort_menu)
        self.browse_button = QPushButton("浏览")
        browse_menu = QMenu(self.browse_button)
        self.discovery.add_browse_actions(browse_menu)
        browse_menu.addSeparator()
        browse_menu.addAction("搜索筛选", self.search_options.click)
        browse_menu.addAction("听歌识曲", self.btn_identify.click)
        self.browse_button.setMenu(browse_menu)
        self.search_options.hide()
        self.btn_identify.hide()
        self.header_layout.addWidget(self.browse_button)
        self.header_layout.addWidget(self.sort_button)
        self.header_layout.addWidget(self.btn_view_toggle)
        self.btn_view_toggle.show()
        self.popular_songs.enable_table()
        self.popular_songs.cards.setContextMenuPolicy(Qt.CustomContextMenu)
        self.popular_songs.cards.customContextMenuRequested.connect(self._popular_context_menu)
        self.list_rank_songs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_rank_songs.customContextMenuRequested.connect(self._rank_context_menu)
        self.btn_view_toggle.setChecked(False)
        self._toggle_view_mode(False)
        self.tabs.currentChanged.connect(self._update_heading)
        self.online_stack.currentChanged.connect(self._update_heading)
        self.tabs.setCurrentIndex(1)
        self._update_heading()

    def _update_heading(self, *_):
        if not hasattr(self, "sort_button"):
            return
        index = self.tabs.currentIndex()
        title = ("本地音乐","发现音乐","最近播放","我的歌单","排行榜")[index]
        if index == 1 and self.online_stack.currentWidget() is self.results_page:
            title = "搜索结果" if self._discovery_tracks is None else "歌单详情"
        if index == 3 and self._favorites_only:
            title = "我的收藏"
        self.heading.setText(title)
        self.browse_button.setVisible(index == 1)
        self.sort_button.setVisible(index != 1 or self.online_stack.currentWidget() is self.results_page)

    def _popular_context_menu(self, pos):
        listing = self.popular_songs.cards
        item = listing.itemAt(pos)
        if item is None:
            return
        listing.setCurrentItem(item)
        group = item.data(Qt.UserRole)
        index = max(0, self.popular_songs.versions.currentIndex())
        track = group.entries[index].track
        self._build_song_menu(self._online_song_row(track, [entry.track for entry in group.entries])).exec(
            listing.viewport().mapToGlobal(pos))

    def _rank_context_menu(self, pos):
        listing = self.list_rank_songs
        item = listing.itemAt(pos)
        if item is not None:
            track = self._rank_tracks[listing.row(item)]
            self._build_song_menu(self._online_song_row(track, ())).exec(listing.viewport().mapToGlobal(pos))

    def _sort_tracks(self, key):
        listing = (self.list_local, self.list_results, self.list_history,
                   self.list_playlist_songs, self.list_rank_songs)[self.tabs.currentIndex()]
        if listing is self.list_rank_songs and not self.rank_mode.isChecked():
            listing = self.popular_songs.cards
        count = listing.count()
        if count < 2:
            return
        reverse = getattr(listing, "_sort_key", None) == (key, False)
        listing._sort_key = (key, reverse)
        selected = listing.currentItem()
        def value(row):
            data = listing.item(row).data(Qt.UserRole) or {}
            if listing is self.popular_songs.cards:
                track = data.entries[0].track
                if key == "duration":
                    return track.duration_seconds or 0
                return str(getattr(track, key, "") or "").casefold()
            if key == "duration":
                try:
                    return float(data.get("duration_seconds") or data.get("duration") or 0)
                except (TypeError, ValueError):
                    return 0.
            return str(data.get(key) or "").casefold()
        order = sorted(range(count), key=value, reverse=reverse)
        with QSignalBlocker(listing):
            items = [listing.takeItem(0) for _ in range(count)]
            for index in order:
                listing.addItem(items[index])
            if selected is not None:
                listing.setCurrentItem(selected)
        names = (("_tracks_by_row","_track_versions") if listing is self.list_results else
                 ("_local_songs",) if listing is self.list_local else
                 ("_rank_tracks",) if listing is self.list_rank_songs else ())
        for name in names:
            rows = getattr(self, name, ())
            if len(rows) == count:
                setattr(self, name, [rows[index] for index in order])
        if listing is self.list_results:
            self._online_selection_changed(listing.currentRow())

    def _focus_search(self):
        """Ctrl+F / Ctrl+K：聚焦当前 tab 的搜索框并全选。"""
        if self.tabs.currentIndex() == 4:
            self.tabs.setCurrentIndex(1)
        box = self.edit_local if self.tabs.currentIndex() == 0 else self.edit_kw
        box.setFocus()
        box.selectAll()

    # ---- 本地 ----
    def _on_scan_local(self):
        """扫描本地音乐：读 music_dirs.json 启用目录 → 应用到 scanner → QThread rescan。"""
        if self._music is None:
            return
        from ..settings_store import ALL_EXTENSIONS, load_music_dirs
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
        # 本地库变化 → 歌词索引失效，下次按歌词搜索时重建
        try:
            self._get_lyric_index().invalidate()
        except Exception:
            pass
        self._load_local_all()

    def _on_scan_failed(self, err: str) -> None:
        self.btn_scan_local.setEnabled(True)
        self.lbl_scan_status.setText(f"扫描失败: {err}")

    def _load_local_all(self):
        self.edit_local.setText("")
        self._refresh_local_list("")

    def _do_local_search(self):
        kw = self.edit_local.text().strip()
        if self.cb_local_mode.currentText() == "按歌词":
            self._do_lyric_search(kw)
        else:
            self._refresh_local_list(kw)

    # ---- 按歌词模糊搜索 ----
    def _get_lyric_index(self) -> LyricIndex:
        """惰性创建歌词索引（db 从 music.controller 取，可为 None 不崩）。"""
        if getattr(self, "_lyric_index", None) is None:
            self._lyric_index = LyricIndex(
                getattr(self._music, "db", None) if self._music else None)
        return self._lyric_index

    def _do_lyric_search(self, kw: str):
        """按歌词搜索：索引未建 → 后台构建后自动继续；已建 → 直接模糊匹配。"""
        if self._music is None:
            return
        idx = self._get_lyric_index()
        if not idx.is_built():
            self.lbl_scan_status.setText("正在建立歌词索引…")
            self._pending_lyric_query = kw
            self._start_lyric_build()
            return
        try:
            songs = idx.search(kw)
        except Exception as e:
            print(f"[Music] lyric search error: {e}")
            songs = []
        self._show_lyric_results(songs, kw)

    def _start_lyric_build(self):
        """后台线程建索引（防重入：已有运行中线程则忽略）。"""
        if (getattr(self, "_lyric_build_thread", None) is not None
                and self._lyric_build_thread.isRunning()):
            return
        idx = self._get_lyric_index()
        thread = QThread()
        worker = _LyricBuildWorker(idx)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.done.connect(self._on_lyric_build_done)
        worker.failed.connect(self._on_lyric_build_failed)
        self._lyric_build_thread = thread
        self._lyric_build_worker = worker
        thread.start()

    def _on_lyric_build_done(self, n: int):
        self.lbl_scan_status.setText(f"歌词索引就绪（{n} 行）")
        q = self._pending_lyric_query
        self._pending_lyric_query = None
        if q:
            self._do_lyric_search(q)  # 索引已建 → 直接匹配

    def _on_lyric_build_failed(self, err: str):
        self.lbl_scan_status.setText(f"歌词索引失败: {err}")
        self._pending_lyric_query = None

    def _show_lyric_results(self, songs: list[dict], kw: str):
        """结果列表复用本地列表（title - artist + UserRole=song，双击走 _on_local_play）。"""
        if not songs:
            self.list_local.clear()
            self._local_songs = []
            self.lbl_scan_status.setText(f"未找到包含该歌词的歌曲：{kw}")
            return
        self._local_songs = songs
        self.list_local.clear()
        for s in songs:
            title = s.get("title", "?")
            artist = s.get("artist", "")
            item = QListWidgetItem(f"{title} - {artist}")
            item.setData(Qt.UserRole, self._card_data(
                s, artist, f"local:{s.get('id')}" if s.get("id") else None))
            self.list_local.addItem(item)
        self.lbl_scan_status.setText(f"歌词匹配 {len(songs)} 首")

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
            item.setData(Qt.UserRole, self._card_data(
                s, artist, f"local:{s.get('id')}" if s.get("id") else None))
            self.list_local.addItem(item)

    def _on_local_play(self, item):
        s = item.data(Qt.UserRole)
        if not s or self._music is None:
            return
        self._play_async(self._music.play_context, tuple(self._local_songs), self.list_local.row(item),
                         title=s.get("title", ""), context=True)

    def _on_highlight(self, on: bool):
        try:
            self._music.set_highlight_mode(on)
        except Exception as e:
            print(f"[Music] highlight toggle: {e}")
        self._sync_highlight()

    def _sync_highlight(self):
        try:
            on = bool(self._music.is_highlight_mode())
        except Exception:
            return
        self.action_highlight.setChecked(on)
        self.action_highlight.setText(f"高潮模式：{'开' if on else '关'}")

    def _on_mark_highlight(self):
        """把当前播放位置保存为人工高潮起点。"""
        if self._music is None:
            return
        try:
            ok = self._music.mark_highlight_start()
        except Exception as e:
            ok = False
            self.lbl_status.setText(f"标记失败: {e}")
            return
        if ok:
            self.lbl_status.setText("已标记当前位置为高潮起点")
        else:
            self.lbl_status.setText("标记失败：没有正在播放的歌曲或位置无效")

    # ---- 倍速 ----
    @staticmethod
    def _speed_label(rate: float) -> str:
        return f"{rate:g}x"

    def _set_sleep_timer(self, seconds):
        if self._music and self._music.set_sleep_timer(seconds):
            self._on_sleep_timer_state(True, seconds)

    def _cancel_sleep_timer(self):
        if self._music:
            self._music.cancel_sleep_timer()
        self._on_sleep_timer_state(False, 0)

    def _on_music_state_event(self, event):
        data = dict(getattr(event, "data", {}) or {})
        if data.get("state") == "sleep_timer":
            self._sleep_timer_signal.emit(bool(data.get("active")), int(data.get("seconds") or 0))
        else:
            self._queue_changed.emit()

    def _on_sleep_timer_state(self, active: bool, seconds: int):
        if not active or not seconds:
            label = "睡眠定时"
        elif seconds % 60:
            label = f"睡眠定时 · 已设 {seconds} 秒"
        else:
            label = f"睡眠定时 · 已设 {seconds // 60} 分钟"
        self._sleep_menu.setTitle(label)

    def _setup_speed_actions(self):
        self._speed_actions = QActionGroup(self._speed_menu)
        for idx, rate in enumerate(self._speed_steps):
            action = self._speed_menu.addAction(self._speed_label(rate))
            action.setData(idx)
            action.setCheckable(True)
            self._speed_actions.addAction(action)
        self._speed_actions.triggered.connect(self._select_speed)
        self._set_speed_display(self._speed_steps.index(1.0))

    def _set_speed_display(self, idx):
        self._speed_idx = idx
        self._speed_actions.actions()[idx].setChecked(True)
        self._speed_menu.setTitle(f"倍速 {self._speed_label(self._speed_steps[idx])}")

    def _select_speed(self, action):
        idx = action.data()
        # Keep the last confirmed speed checked while the request is in flight.
        self._set_speed_display(self._speed_idx)
        self._apply_speed(self._speed_steps[idx], idx)

    def _sync_speed(self):
        try:
            idx = self._speed_steps.index(self._music.get_speed())
        except Exception:
            return
        if idx != self._speed_idx:
            self._set_speed_display(idx)

    def _apply_speed(self, rate: float, idx: int | None = None):
        """应用倍速档位。仅明确成功才更新 UI 显示。
        set_speed 会走 mpv IPC（最长 3s 超时），放 daemon 线程避免卡 UI；
        结果用 Signal 回主线程更新按钮文字。"""
        if self._music is None:
            return
        if idx is None:
            try:
                idx = self._speed_steps.index(rate)
            except ValueError:
                return

        def _do():
            try:
                ok = self._music.set_speed(rate)
                err = ("" if ok is True else "倍速设置失败" if ok is False
                       else "暂时无法确认倍速设置结果，请检查播放状态")
            except TimeoutError:
                ok = None
                err = "倍速设置超时，暂时无法确认结果"
            except Exception as e:
                ok = False
                err = f"倍速失败: {e}"
            # 跨线程回主线程（QTimer.singleShot 从后台线程调用不绑定主线程事件循环）
            self._speed_result.emit(ok, rate, idx, err)

        threading.Thread(target=_do, daemon=True,
                         name="ui-music-speed").start()

    def _after_speed(self, ok: bool | None, rate: float, idx: int, err: str):
        """主线程槽：根据后台实际结果更新按钮文字。"""
        if ok is True:
            self._set_speed_display(idx)
        else:
            self.lbl_status.setText(err)

    # ---- 播放控制 ----
    def _safe_call(self, fn, *args) -> None:
        """后台线程通用包裹：controller 调用放子线程，避免 mpv IPC 超时卡住 UI；
        异常捕获记录，绝不炸线程。"""
        try:
            fn(*args)
        except Exception as e:
            print(f"[MusicPanel] 后台调用失败: {e!r}")

    def _enter_switching_ui(self) -> None:
        """切歌意图发出瞬间：UI 立即归零并显示"加载中"（不等后台轮询确认）。
        真正的渲染恢复由 controller._is_switching_track 翻转驱动（_on_progress），
        本方法只负责"立刻"反馈，避免旧歌进度残留在切换窗口内。"""
        self._cancel_manual_feedback()
        try:
            self.slider_pos.setValue(0)
            self.slider_pos.set_chorus(None)
            self.lbl_time.setText("加载中...")
        except Exception:
            pass

    def _cancel_manual_feedback(self):
        request = getattr(self, "_manual_request", None)
        if request is not None:
            request[0].set()
            self._manual_request = None

    def _play_async(self, target, *args, title="", resolving=False,
                    fail_msg=PLAY_RETRY, status_label=None, play_options=None, context=False):
        from ...music.next_feedback import observe_manual_play

        if self._music is None:
            return
        self._enter_switching_ui()
        if not play_options:
            self._retry_online_track = None
        self.btn_retry_online.hide()
        label = status_label or self.lbl_status
        cancel = threading.Event()
        intent_ns = time.perf_counter_ns()
        self._manual_request = (cancel, label, title, fail_msg, intent_ns)
        label.setText(PLAY_LOADING)

        def publish(stage, epoch):
            if not cancel.is_set():
                try:
                    self._manual_play_status.emit(cancel, epoch, stage)
                except RuntimeError:
                    cancel.set()  # Widget was destroyed during a slow resolver call.

        if context or play_options:
            original = target
            options = dict(play_options or {}, _before_load=lambda: not cancel.is_set(), _intent_ns=intent_ns)
            target = lambda *values: original(*values, **options)
        threading.Thread(target=observe_manual_play,
                         args=(self._music, target, args, cancel, publish),
                         daemon=True, name="ui-music-play-async").start()

    def _on_manual_play_status(self, cancel, epoch, stage):
        req = self._manual_request
        if req is None or req[0] is not cancel or cancel.is_set():
            return
        _, label, title, failure, intent = req
        music = self._music
        if ((epoch is not None and getattr(music, "_play_request_id", None) != epoch)
                or (hasattr(music, "_movie_permits_request") and not music._movie_permits_request(intent))):
            self._cancel_manual_feedback()
            return
        if stage == "started" and (not music._running or music._last_loadfile_epoch != epoch):
            self._cancel_manual_feedback()
            return
        messages = {
            "loading": PLAY_LOADING,
            "accepted": PLAY_LOADING,
            # This confirms startup; the playback bar owns live pause and resume state.
            "started": f"已开始播放：{title}" if title else "歌曲已开始播放。",
            "unknown": "暂未确认播放，请查看播放器或重试。",
            "no_url": PLAY_RETRY,
            "failed": failure,
        }
        label.setText(messages[stage])
        self.btn_retry_online.setVisible(label is self.lbl_status and stage in ("failed", "no_url", "unknown")
                                         and self._retry_online_track is not None)
        if stage not in ("loading", "accepted"):
            if stage != "started" and self.lbl_time.text() == "加载中...":
                self.lbl_time.setText("0:00 / 0:00")
            self._cancel_manual_feedback()

    def _on_play_result(self, ok: bool, msg: str, lbl) -> None:
        if not ok:
            lbl.setText(msg)

    def _on_prev(self):
        if self._music is None:
            return
        self._enter_switching_ui()
        threading.Thread(target=self._safe_call, args=(self._music.prev,),
                         daemon=True, name="ui-music-prev").start()

    def _sync_toggle_icon(self):
        """按真实播放状态立即同步 播放/暂停 图标（锁定期间跳过，防止点击后闪回）。"""
        if getattr(self, "_icon_locked", False):
            return
        try:
            playing = bool(self._music.is_playing())
        except Exception:
            playing = None
        # 波形进度条：播放中跳动、暂停低电平
        try:
            if hasattr(self, "_wave_slider"):
                self._wave_slider.set_active(bool(playing))
        except Exception:
            pass
        if playing is not None:
            want = "pause" if playing else "play"
            if getattr(self, "_toggle_icon", None) != want:
                self._toggle_icon = want
                self.btn_toggle.setIcon(lucide_icon(want, 18, "#ffffff"))

    def _on_toggle(self):
        if self._music is None:
            return
        # 点击瞬间锁定图标并立即翻转，mpv 异步启动期间旧状态不会再把它闪回
        cur = getattr(self, "_toggle_icon", None)
        if cur is None:                    # 首次点击：先对齐真实状态再翻转
            self._sync_toggle_icon()
            cur = getattr(self, "_toggle_icon", "play")
        self._icon_locked = True
        want = "pause" if cur == "play" else "play"
        self._toggle_icon = want
        self.btn_toggle.setIcon(lucide_icon(want, 18, "#ffffff"))
        threading.Thread(target=self._safe_call, args=(self._music.toggle,),
                         daemon=True, name="ui-music-toggle").start()
        # 轮询真实播放状态直到确认翻转（mpv 异步），最长 ~3s；切换失败则按真实状态校正
        self._poll_ticks = 15
        QTimer.singleShot(200, self._poll_toggle_confirm)

    def _poll_toggle_confirm(self):
        """轮询真实播放状态直到确认翻转（mpv 异步启动中 is_playing 短暂仍是旧值，
        此时继续等待而不是误判为切换失败）；超过上限才判定失败并按真实状态校正。"""
        target_playing = self._toggle_icon == "pause"
        try:
            playing = bool(self._music.is_playing())
        except Exception:
            playing = None
        if playing == target_playing:
            self._icon_locked = False
            return
        if self._poll_ticks > 0:
            self._poll_ticks -= 1
            QTimer.singleShot(200, self._poll_toggle_confirm)
        else:
            # 超时仍未翻转 → 切换未生效（mpv 不可用/落雪回落等）→ 解锁并按真实状态校正
            self._icon_locked = False
            self._sync_toggle_icon()

    def _select_play_mode(self, action):
        try:
            mode = action.data()
            if self._music.get_play_mode() != mode:
                self._music.set_play_mode(mode)
        except Exception:
            logger.exception("播放模式更新失败")
        self._sync_play_mode()

    def _set_play_mode_display(self, mode):
        for action in self._mode_actions.actions():
            if action.data() == mode:
                action.setChecked(True)
                self.btn_mode.setText(action.text())
                self.btn_mode.setToolTip(f"播放模式：{action.text()}")
                return

    def _sync_play_mode(self):
        try:
            mode = self._music.get_play_mode()
        except Exception:
            return
        self._set_play_mode_display(mode)

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
            # 真后台线程：clear_queue 在主线程持 controller._lock + mpv.stop()（IPC），
            # 直接/同步 _safe_call 都会卡住 GUI——daemon 线程执行（对齐 next/toggle 路径）。
            threading.Thread(target=self._safe_call, args=(self._music.clear_queue,),
                             daemon=True, name="ui-music-clear").start()
        except Exception as e:
            print(f"[MusicPanel] 清空队列失败: {e}")

    def _on_next(self):
        if self._music is None:
            return
        self._enter_switching_ui()
        threading.Thread(target=self._safe_call, args=(self._music.next,),
                         daemon=True, name="ui-music-next").start()

    def _on_volume(self, v):
        if self._music is None or self._syncing_volume:
            return
        self._volume_request_gen += 1
        threading.Thread(target=self._safe_call,
                         args=(self._music.set_volume, int(v)),
                         daemon=True, name="ui-music-volume").start()

    def _on_volume_released(self):
        """音量滑块松开 → 防抖 1s 写回 settings.yaml music.default_volume
        （拖动过程 valueChanged 已实时 set_volume，只在松手后落盘一次；ducking
        恢复调 set_volume 不碰滑块 → 不会误写）。"""
        try:
            from app.ui.settings_store import save_settings_yaml
            value = int(self.slider_vol.value())
            QTimer.singleShot(1000, lambda: save_settings_yaml({"music": {"default_volume": value}}))
        except Exception as e:
            print(f"[MusicPanel] 音量写回配置失败: {e!r}")

    # ---- 在线 ----
    def refresh_online_sources(self, *, include_discovery=True) -> None:
        """刷新在线音源下拉（插件/自定义源变化后由外部调用）。

        重建「全部」+ 当前注册源列表；保留用户当前选中项，若已失效则回退「全部」。
        不触发 currentIndexChanged 副作用（blockSignals），不影响正在进行的播放。
        """
        if self._music is None:
            return
        try:
            cur = self.cb_source.currentData()
            names = self._music.get_online_sources() or []
            self.cb_source.blockSignals(True)
            try:
                self.cb_source.clear()
                self.cb_source.addItem("全部", userData=None)
                for name in names:
                    self.cb_source.addItem(name, userData=name)
                if cur is not None:
                    idx = self.cb_source.findData(cur)
                    if idx >= 0:
                        self.cb_source.setCurrentIndex(idx)
            finally:
                self.cb_source.blockSignals(False)
            if include_discovery and self._search_pages is not None:
                self._poll_search_pages()
            if include_discovery and hasattr(self, "discovery"):
                self.discovery.refresh_sources()
                self.popular_songs.cache.clear()
                if self.popular_songs.isVisible():
                    self.popular_songs.load(force=True)
        except Exception as e:
            print(f"[MusicPanel] 刷新音源列表失败: {e}")

    @Slot()
    def _request_search(self):
        """Qt clicked(bool) 不进入业务完成回调参数。"""
        self._do_search(rank_for_display=True)

    def _stop_search_pages(self):
        if self._search_pages is None:
            return
        self._search_pages = None
        self._search_page_timer.stop()
        self.btn_search_more.hide()
        self.btn_search_retry.hide()
        notice_id = getattr(self, "_search_notice_ids", {}).pop(self._search_generation, None)
        if notice_id:
            center = get_notification_center()
            notice = center.get(notice_id)
            if notice and notice.status == "processing":
                center.update(notice_id, title="已结束本次搜索", status="warning", phase="done")

    def _search_keyword_changed(self, *_):
        if self._search_pages is not None:
            self._stop_search_pages()
            self._last_online_result = None
            self.lbl_status.setText("关键词已更改，请点击搜索或按回车")

    def _poll_search_pages(self):
        session = self._search_pages
        if session is not None and session.poll():
            self._render_search_pages()
        if session is None or not session.loading():
            self._search_page_timer.stop()

    def _render_search_pages(self):
        session = self._search_pages
        results = session.results()
        self._on_search_done(results, display_query=session.keyword, paged=True)
        owner = self.cb_source.currentData()
        loading = session.loading(owner)
        errors = session.errors(owner)
        count = len(self._tracks_by_row)
        if loading:
            status = f"已找到 {count} 首，仍在搜索…" if count else SEARCHING
        elif errors:
            if count:
                status = f"已找到 {count} 首；部分内容暂不可用。"
            elif set(errors.values()) <= {"unavailable", "interrupted"}:
                status = "暂无可用音源，请检查音源后重新搜索。"
            elif set(errors.values()) <= {"timeout", "network_timeout", "busy"}:
                status = "部分音源响应超时，请重试。"
            else:
                status = "搜索暂未完成，请重试。"
        elif count:
            status = f"已找到 {count} 首"
        elif (results.successful_sources if owner is None else owner in results.successful_sources):
            status = "未找到歌曲，试试其他歌名或歌手"
        else:
            status = "暂无可用音源，请检查音源设置"
        if self._manual_request is None:
            self.lbl_status.setText(status)
        can_load = self._search_has_buffer or session.can_fetch(owner)
        self.btn_search_more.setVisible(can_load and (not loading or self._search_has_buffer))
        self.btn_search_retry.setVisible(not loading and session.can_retry(owner))

        if not session.loading():
            notice_id = self._search_notice_ids.pop(self._search_generation, None)
            if notice_id:
                notify(f"已找到 {count} 首歌曲" if count else status, status,
                       operation_id=notice_id, status="success" if count and not session.errors() else "warning")

    def _load_search_more(self):
        session = self._search_pages
        if session is None:
            return
        self._search_visible_limit += 30
        owner = self.cb_source.currentData()
        self._render_search_pages()
        if len(self._tracks_by_row) < self._search_visible_limit:
            session.request(owner)
            self._poll_search_pages()
            self._render_search_pages()
            if session.loading():
                self._search_page_timer.start()

    def _retry_search_pages(self):
        session = self._search_pages
        if session is not None:
            session.request(self.cb_source.currentData(), retry=True)
            self._poll_search_pages()
            self._render_search_pages()
            if session.loading():
                self._search_page_timer.start()

    def _do_search(self, *, on_done=None, on_failed=None, rank_for_display=False):
        if hasattr(self, "sort_button"):
            self.tabs.setCurrentIndex(1)
        kw = self.edit_kw.text().strip()
        if not kw or self._music is None:
            return
        self._stop_search_pages()
        self._discovery_tracks = None
        self._retry_online_track = None
        self.btn_retry_online.hide()
        self._results_requested = True
        self.online_stack.setCurrentWidget(self.results_page)
        self.cb_search_filter.setEnabled(True)
        self.btn_discovery_more.hide()
        self.discovery._task.invalidate()
        self._search_generation += 1
        center = get_notification_center()
        notice_ids = getattr(self, "_search_notice_ids", {})
        for previous in notice_ids.values():
            old = center.get(previous)
            if old and old.status == "processing":
                center.update(previous, title="已改为新的搜索", status="warning", phase="done")
        operation_id = current_operation_id()
        if operation_id:
            center.update(operation_id, title="正在搜索可用音源", phase="running")
        else:
            operation_id = center.post("正在搜索歌曲", f"搜索：{kw}", source="music")
        self._search_notice_ids = {self._search_generation: operation_id}
        request = (self._search_generation, kw, on_done, on_failed, kw if rank_for_display else "")
        self.list_results.clear()
        self._tracks_by_row = []
        self.lbl_status.setText(SEARCHING)
        open_search = getattr(self._music, "open_online_search", None)
        if rank_for_display and on_done is None and on_failed is None and open_search is not None:
            session = open_search(kw)
            if session is not None:
                self._pending_search = None
                self._search_pages = session
                self._search_visible_limit = 30
                self.refresh_online_sources(include_discovery=False)
                session.request()
                self._poll_search_pages()
                self._render_search_pages()
                if session.loading():
                    self._search_page_timer.start()
                return
        # finished 槽回收旧线程前不覆盖引用；最多保存最后一次待执行请求。
        if self._thread is not None:
            self._pending_search = request
            return
        self._start_search(request)

    def _start_search(self, request):
        generation, kw, _, _, _ = request
        self._active_search = request
        # 搜索前同步一次音源列表（插件导入/刷新后无需重启即生效；
        # 保留选中项，失效自动回退「全部」，不影响正在播放的音乐）
        # Search only needs the source selector. Do not restart a visible
        # recommendation read every time the user searches for a song.
        self.refresh_online_sources(include_discovery=False)
        self._thread = QThread()
        self._worker = _SearchWorker(self._music, kw, generation)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_search_worker_done, Qt.QueuedConnection)
        self._worker.failed.connect(self._on_search_worker_failed, Qt.QueuedConnection)
        self._worker.done.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_search_thread_finished, Qt.QueuedConnection)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot(int, object)
    def _on_search_worker_done(self, generation, result):
        request = self._active_search
        if (request is not None and request[0] == generation
                and request[1] == self.edit_kw.text().strip()):
            self._on_search_done(result, generation, request[2], request[3], display_query=request[4])

    @Slot(int, str)
    def _on_search_worker_failed(self, generation, error):
        request = self._active_search
        if (request is not None and request[0] == generation
                and request[1] == self.edit_kw.text().strip()):
            self._on_search_failed(error, generation, request[3])

    @Slot()
    def _on_search_thread_finished(self):
        request = self._active_search
        self._thread = None
        self._worker = None
        self._active_search = None
        pending, self._pending_search = self._pending_search, None
        if self._search_pages is not None:
            return
        if pending is not None and pending[1] == self.edit_kw.text().strip():
            self._start_search(pending)
        elif pending is not None or (request is not None and request[1] != self.edit_kw.text().strip()):
            self.lbl_status.setText("关键词已更改，请点击搜索或按回车")

    @staticmethod
    def _search_error_hint(errors):
        categories = set(errors.values())
        if "interrupted" in categories:
            return "音源已刷新或变更，请重试搜索"
        if categories & {"timeout", "network_timeout"}:
            return "请求超时，请检查网络后重试，或更换音源"
        if any(str(category).startswith(("network", "http_")) for category in categories):
            return "音源连接失败，请检查网络后重试，或更换音源"
        return "请重试或更换音源；若持续失败，请在设置中检查插件和网络"

    def _on_search_done(self, result: dict, search_generation=None, on_done=None,
                        on_failed=None, *, display_query="", paged=False):
        if search_generation is not None and search_generation != self._search_generation:
            return
        self._last_online_result = result
        self._last_online_query = display_query
        if getattr(self, "_results_requested", False):
            self.online_stack.setCurrentWidget(self.results_page)
        self._voice_generation = None   # 手动搜索 → 重置语音候选代际
        selected = self.list_results.currentRow()
        previous = self._tracks_by_row[selected] if paged and 0 <= selected < len(self._tracks_by_row) else None
        scroll = self.list_results.verticalScrollBar()
        offset = scroll.value()
        self.list_results.clear()
        source_filter = self.cb_source.currentData()
        version_filter = self.cb_search_filter.currentIndex() if hasattr(self, "cb_search_filter") else 0
        markers = ("现场", "live") if version_filter == 1 else ("翻唱", "cover")
        collection = getattr(self, "_discovery_tracks", None)
        tracks = collection if collection is not None else [
            track for source, rows in result.items()
            if not source_filter or source == source_filter for track in rows
        ]
        tracks_for_search = [
            track for track in tracks
            if collection is None or not source_filter or track.source_name == source_filter
            if not version_filter or any(mark in track.title.casefold() for mark in markers)
        ]
        if display_query:
            tracks_for_search = rank_manual_search(tracks_for_search, display_query)
        from ...music.online.content_choices import group_tracks
        self._track_versions = (group_tracks(tracks_for_search) if on_done is None and collection is None
                                else [[track] for track in tracks_for_search])
        if paged:
            self._search_has_buffer = len(self._track_versions) > self._search_visible_limit
            self._track_versions = self._track_versions[:self._search_visible_limit]
        tracks_for_search = [group[0] for group in self._track_versions]
        n = len(tracks_for_search)
        for t in tracks_for_search:
            # 显示具体插件源名（t.source_name = 插件 platform，如"酷我音乐"），
            # 而不是 dict key "js_bridge"（7 个 JS 插件合并注册成一个源）
            item = QListWidgetItem(f"{t.title} — {t.artist}")
            sub = " · ".join(str(value) for value in (t.artist, t.album) if value)
            # 登记在线封面 → 历史/歌单缺字段时补全
            if self._music is not None and hasattr(self._music, "remember_cover"):
                try:
                    self._music.remember_cover(
                        title=t.title or "", artist=t.artist or "",
                        source_name=t.source_name or "",
                        track_id=t.track_id or "",
                        cover_url=getattr(t, "cover_url", None) or "")
                except Exception:
                    pass
            item.setData(Qt.UserRole, self._card_data({
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "duration_seconds": t.duration_seconds,
                "source_name": t.source_name,
                "track_id": t.track_id,
                "cover_url": getattr(t, "cover_url", None) or None,
            }, sub, f"net:{t.track_id}" if t.track_id else None))
            item.setToolTip(" · ".join(filter(None, (t.title, t.artist, t.album, "来源：" + t.source_name))))
            item.setData(MusicCardRoles.DownloadRole, True)
            self.list_results.addItem(item)
        self._tracks_by_row = tracks_for_search
        self._fit_sparse_results()
        for key, task in self._download_tasks.items():
            self._update_download_card(key, task)
        row = next((i for i, group in enumerate(self._track_versions) if previous is not None
                    and any(previous is track for track in group)), 0 if tracks_for_search else -1)
        self.list_results.setCurrentRow(row)
        if paged:
            scroll.setValue(offset)
            self._online_selection_changed(row)
            return
        errors = {src: error for src, error in getattr(result, "errors", {}).items()
                  if not source_filter or src == source_filter}
        succeeded = {src for src in getattr(result, "successful_sources", set())
                     if not source_filter or src == source_filter}
        if errors and not succeeded:
            self._on_search_failed(self._search_error_hint(errors), search_generation, on_failed)
            return
        if errors:
            self.lbl_status.setText(f"已找到 {n} 首；部分内容暂不可用，可重试搜索。")
        elif hasattr(result, "successful_sources") and not succeeded:
            self._on_search_failed("暂无可用音源，请在设置中检查插件或添加音源", search_generation, on_failed)
            return
        else:
            self.lbl_status.setText(f"已找到 {n} 首" if n else "未找到歌曲，试试其他歌名或歌手")
        notice_id = getattr(self, "_search_notice_ids", {}).get(search_generation)
        if notice_id:
            notify(f"已找到 {n} 首歌曲" if n else "没有找到歌曲", self.lbl_status.text(),
                   operation_id=notice_id, status="warning" if errors or not n else "success",
                   advice="可换一个关键词或音源重试" if errors or not n else "")
        if on_done is not None:
            on_done(list(tracks_for_search))

    def _on_search_failed(self, err: str, search_generation=None, on_failed=None):
        if search_generation is not None and search_generation != self._search_generation:
            return
        self._retry_online_track = None
        logger.warning("music search failed: %s", err)
        self.lbl_status.setText("搜索暂未完成，请重试。")
        self.btn_retry_online.setVisible(True)
        notice_id = getattr(self, "_search_notice_ids", {}).get(search_generation)
        if notice_id:
            notify("歌曲搜索未完成", "音源没有返回可用结果", status="failed",
                   operation_id=notice_id, advice="请检查网络，重试或换一个音源")
        if on_failed is not None:
            on_failed(err)

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
            if self._search_pages is not None:
                self._stop_search_pages()
                self._search_generation += 1
            self._voice_generation = gen
            self._results_requested = True
            self._last_online_result = None
            self._discovery_tracks = None
            if hasattr(self, "online_stack"):
                self.online_stack.setCurrentWidget(self.results_page)
                self.cb_search_filter.setEnabled(False)
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
                item = QListWidgetItem(f"{title} — {artist}")
                sub = artist
                item.setToolTip("来源：" + str(src))
                item.setData(Qt.UserRole, self._card_data({
                    "title": title,
                    "artist": artist,
                    "source_name": src,
                    "cover_url": row.get("cover_url"),
                }, sub, None))
                self.list_results.addItem(item)
            n = len(display)
            if n:
                self.lbl_status.setText(f"共找到 {n} 首候选，双击播放")
            else:
                self.lbl_status.setText("未找到候选")
        except Exception as e:
            print(f"[MusicPanel] 候选渲染失败: {e}")

    def _on_tab_changed(self, index: int):
        """切到历史/歌单/榜单 tab → 刷新（构造后不再重读数据库的补偿）。"""
        if index == 2:
            self._refresh_history()
        elif index == 3:
            self._refresh_playlists()
        elif index == 4 and self.rank_mode.isChecked():
            self._load_top_lists()

    def _rank_mode_changed(self, by_source):
        self.rank_pages.setCurrentIndex(int(by_source))
        self.rank_mode.setText("返回多榜热门" if by_source else "按来源查看")
        if by_source:
            self._load_top_lists()

    def _play_popular_song(self, tracks, index):
        if self._music is not None:
            self._play_async(self._music.play_online_list, tracks, index,
                             title=tracks[index].title, resolving=True, context=True,
                             fail_msg="该曲目无法播放，可选择其他版本",
                             status_label=self.popular_songs.play_status)

    def _show_discovered_tracks(self, tracks, title):
        self._stop_search_pages()
        self._results_requested = True
        self.cb_search_filter.setEnabled(True)
        self.cb_search_filter.blockSignals(True)
        self.cb_search_filter.setCurrentIndex(0)
        self.cb_search_filter.blockSignals(False)
        self._search_generation += 1
        self._pending_search = None
        # Discovery results carry the actual plugin name, not a merged search key.
        with QSignalBlocker(self.cb_source):
            self.cb_source.setCurrentIndex(0)
        self._discovery_tracks = tuple(tracks)
        grouped = {}
        for track in tracks:
            grouped.setdefault(track.source_name, []).append(track)
        self._on_search_done(grouped)
        self.lbl_status.setText(f"{title} · {len(tracks)} 首；双击播放，右键可加入队列或歌单")

    def _show_discovery_home(self):
        if self._search_pages is not None:
            self._stop_search_pages()
            self._last_online_result = None
        self._results_requested = False
        self.online_stack.setCurrentWidget(self.discovery)
        self.discovery.show_view("home")
        self.search_options.setChecked(False)
        self.btn_discovery_more.hide()

    def _apply_online_filter(self, *_):
        if self._search_pages is not None:
            self._search_pages.poll()
            self._render_search_pages()
        elif self._last_online_result is not None:
            self._on_search_done(self._last_online_result, display_query=self._last_online_query)

    def _play_discovery_history(self, row):
        item = QListWidgetItem()
        item.setData(Qt.UserRole, row)
        self._on_history_play(item)

    def _open_discovery_playlist(self, row):
        self.tabs.setCurrentIndex(3)
        self._refresh_playlists(preferred_id=row.get("id"))

    def _build_lyrics_menu(self):
        from ..settings_store import load_lyrics_prefs
        prefs=load_lyrics_prefs();self.lyrics_menu.clear();self.lyrics_actions={}
        choices=[("字号放大",{"font_size":min(48,int(prefs["font_size"])+2)}),
                 ("字号缩小",{"font_size":max(12,int(prefs["font_size"])-2)}),
                 ("歌词提前 0.2 秒",{"offset_ms":max(-10000,int(prefs.get("offset_ms",0))-200)}),
                 ("歌词延后 0.2 秒",{"offset_ms":min(10000,int(prefs.get("offset_ms",0))+200)}),
                 ("恢复同步",{"offset_ms":0}),("单行歌词",{"mode":"single"}),
                 ("三行歌词",{"mode":"triple"}),("锁定桌面歌词",{"locked":not prefs["locked"]}),
                 ("恢复窗口位置",{"pos_x":None,"pos_y":None})]
        for title,update in choices:
            action=self.lyrics_menu.addAction(title)
            if title=="锁定桌面歌词":action.setCheckable(True);action.setChecked(prefs["locked"])
            action.triggered.connect(lambda _=False,values=update:self._change_lyrics_prefs(values))
            self.lyrics_actions[title]=action
        low = self.lyrics_menu.addAction("低性能舞台")
        low.setCheckable(True)
        low.setChecked(prefs.get("stage_low_power") is True)
        low.triggered.connect(lambda checked: self._change_lyrics_prefs({"stage_low_power": checked}))
        self.lyrics_actions["低性能舞台"] = low
        motion_action = self.lyrics_menu.addAction("减少歌词动效")
        motion_action.setCheckable(True)
        motion_action.setChecked(prefs.get("reduce_motion") is True)
        motion_action.triggered.connect(lambda checked: self._change_lyrics_prefs({"reduce_motion": checked}))
        self.lyrics_actions["减少歌词动效"] = motion_action
        offset=int(prefs.get("offset_ms",0))
        label=self.lyrics_menu.addAction("同步："+(f"延后 {offset/1000:g} 秒" if offset>0 else f"提前 {-offset/1000:g} 秒" if offset<0 else "未校正"))
        label.setEnabled(False)

    def _change_lyrics_prefs(self,update):
        from ..settings_store import save_lyrics_prefs
        from .settings_panel import _notify_lyrics_prefs_changed
        if save_lyrics_prefs(update):
            self.playing_lyrics.remember_motion_preferences(update)
            _notify_lyrics_prefs_changed(update)
            if self.playing_lyrics.navigation_motion_reduced():
                self._drawer_transition.finish()
            if update.get("reduce_motion"):
                self._content_pages.finish_transition()
        else:
            notify("歌词设置未能保存","请重试",status="failed",source="music")

    def _ensure_lyrics_window(self):
        from .settings_panel import register_lyrics_prefs_listener
        if self._lyrics_window is None:
            from ..widgets.lyrics_window import LyricsWindow
            self._lyrics_window = LyricsWindow(self._music, self)
            self.playing_lyrics.attach(self._lyrics_window)
            self._lyrics_window.lyrics_changed.connect(self._discard_stale_lyric_frame)
        register_lyrics_prefs_listener(self._lyrics_window.apply_prefs)
        return self._lyrics_window

    def _queue_entry_next(self, index, revision):
        success = self._music.replay_queue_entry_next(index, revision)
        self._queue_dialog.action_status.setText("已安排下一首临时插播" if success else "队列已变化，请重新选择")
        self._schedule_queue_refresh()

    def _position_media_pane(self):
        space = self._content_pages.rect()
        inset = 6
        width = min(max(280, space.width() // 2), 372, max(0, space.width() - 2 * inset))
        self._media_pane.setGeometry(max(0, space.width() - width - inset), inset,
                                     width, max(0, space.height() - 2 * inset))

    def _open_media_pane(self, content):
        self._toggle_playing_lyrics(False, animate=False)
        was_closed = self._media_pane.isHidden()
        switched = self._media_pane.currentWidget() is not content
        if was_closed:
            self._drawer_focus = self.focusWidget()
        self._media_pane.setCurrentWidget(content)
        content.show()
        self._position_media_pane()
        self._media_pane.show()
        self._media_pane.raise_()
        if (was_closed or switched) and self.isVisible():
            content.btn_close.setFocus(Qt.OtherFocusReason)
            if not self.playing_lyrics.navigation_motion_reduced():
                self._drawer_transition.wash(self._media_pane, Tokens.BG_BASE, 72, direction=-1)

    def _close_media_pane(self, *, animate=True):
        interrupted = self._drawer_transition.isVisible()
        self._drawer_transition.finish()
        focused = self.focusWidget()
        return_focus = focused is not None and self._media_pane.isAncestorOf(focused)
        was_open = self._media_pane.isVisible()
        self._media_pane.hide()
        if animate and was_open and not interrupted and not self.playing_lyrics.navigation_motion_reduced():
            self._drawer_transition.wash(self._media_pane, Tokens.BG_ELEVATED, 42, direction=-1)
        opener, self._drawer_focus = self._drawer_focus, None
        if return_focus and opener is not None and opener.isVisible():
            opener.setFocus(Qt.OtherFocusReason)

    def _shared_lyric_cover(self, entering):
        from ..widgets.music_card_delegate import cover_key_of, _get_cover_loader
        stage = self.playing_lyrics.stage
        if self._music is None or self.playing_lyrics.pages.currentWidget() is not stage:
            return None
        track = self._music.get_current_song()
        key = cover_key_of(track)
        if not key:
            self._cover_anchor = None
            return None
        stack = self._content_pages

        def stage_anchor():
            return stage.cover_rect().translated(stage.mapTo(stack, QPoint()))

        if entering:
            self._cover_anchor = None
            for listing in (self.list_local, self.list_results, self.list_playlist_songs,
                            self.list_history, self.list_rank_songs):
                item = listing.currentItem()
                if not listing.isVisible() or item is None or listing.viewMode() != QListView.IconMode:
                    continue
                data = item.data(Qt.UserRole)
                if cover_key_of(data) != key or data.get("_play_key") != self._play_key(track):
                    continue
                rect = MusicCardDelegate.cover_rect(listing.visualItemRect(item))
                if listing.viewport().rect().contains(rect.toRect()):
                    self._cover_anchor = key, rect.translated(listing.viewport().mapTo(stack, QPoint())), stack.size()
                break
        anchor = self._cover_anchor
        if not anchor or anchor[0] != key or anchor[2] != stack.size():
            return None
        art = _get_cover_loader().get(key)
        if art is None or art.isNull():
            return None
        origin = anchor[1]
        return (lambda: (art, origin, stage_anchor())) if entering else (art, stage_anchor(), origin)

    def _discard_stale_lyric_frame(self):
        key = self._lyrics_window.current_track.get("key", "")
        self._content_pages.discard_stale_frame(key)

    def _toggle_playing_lyrics(self, enabled, *, animate=True):
        from ..widgets.lyrics_window import LyricsWindow
        if enabled:
            self._close_media_pane(animate=False)
        window = self._ensure_lyrics_window() if enabled and self._music is not None else None
        self.btn_playing_lyrics.setChecked(enabled)
        if enabled and self._content_pages.currentWidget() is not self.playing_lyrics:
            self.playing_lyrics.pages.setCurrentWidget(self.playing_lyrics.stage)
        self._content_pages.transition_to(
            self.playing_lyrics if enabled else self._music_content,
            animate=animate and not self.playing_lyrics.motion_reduced(),
            shared=self._shared_lyric_cover(enabled) if animate and not self.playing_lyrics.motion_reduced() else None,
            direction=1 if enabled else -1,
            track_key=LyricsWindow._song_key(self._music.get_current_song()) if animate and self._music else "",
        )
        if window is not None:
            already_started = window._started
            window.start()
            # start() publishes fresh lyrics. An already running desktop window is reused.
            if already_started:
                self.playing_lyrics.refresh()
        elif self._lyrics_window is not None and not self.btn_lyrics.isChecked():
            self._lyrics_window.stop()

    def _on_toggle_lyrics(self, checked):
        """Desktop visibility is independent of the current-song lyrics view."""
        if self._music is None:
            self.btn_lyrics.setChecked(False)
            return
        window = self._ensure_lyrics_window()
        if checked:
            window.start()
            window.show()
        else:
            window.hide()
            if not self.btn_playing_lyrics.isChecked():
                window.stop()

    # ------------------------------------------------------------------ 哼唱识曲
    def _on_identify_cmd(self, command):
        """语音命令 MUSIC_IDENTIFY 处理器（voice 线程）→ 跨线程桥到主线程弹窗。"""
        self._identify_trigger.emit()

    def _get_identify_service(self) -> HummingService:
        """惰性创建识曲服务（scan_dirs / 指纹库路径 / 扩展名来自 music 配置）。"""
        if self._identify_service is None:
            music = self._music
            # 音乐目录的权威用户配置保存在 music_dirs.json；安装版的
            # settings.yaml 通常只保留空的 scan_dirs 模板，不能只读 controller
            # 初始化时的快照，否则识曲建库会扫描到 0 首。
            from ..settings_store import load_music_dirs
            configured_dirs = [d.get("path") for d in load_music_dirs()
                               if d.get("enabled") and d.get("path")]
            scan_dirs = configured_dirs or list(getattr(music, "scan_dirs", None) or [])
            config = getattr(music, "config", None) or {}
            from app.core.path_resolve import USER_DATA_DIR, resolve_user_path
            configured_db = config.get("fingerprints")
            if configured_db:
                db_path = resolve_user_path(str(configured_db))
            else:
                db_path = str(USER_DATA_DIR / "data" / "music" / "fingerprints.db")
            ext = list(config.get("extensions") or []) or None
            self._identify_service = HummingService(
                scan_dirs=scan_dirs, sqlite_path=db_path, extensions=ext)
        return self._identify_service

    def _on_identify_click(self):
        """按钮或语音触发识曲：显示弹窗并开始（弹窗内部防重入）。"""
        try:
            if self._identify_dialog is None:
                self._identify_dialog = IdentifyDialog(
                    self._get_identify_service(), on_play=self._on_identify_play,
                    on_search=self._on_identify_search,
                    on_begin=self._invalidate_identify_auto_play, parent=self)
            self._identify_dialog.show()
            self._identify_dialog.raise_()
            self._identify_dialog.start()
            self.lbl_identify_status.clear()
            self.lbl_identify_status.hide()
        except Exception as exc:
            logger.exception("识曲窗口打开失败")
            self._identify_dialog = None
            self.lbl_identify_status.setText(f"识曲暂不可用：{exc}")
            self.lbl_identify_status.show()

    def _on_identify_search(self, keyword):
        """识曲结果复用现有在线搜索链路，并在结果完成后自动尝试播放。"""
        generation = self._identify_auto_generation
        self.edit_kw.setText(keyword)
        self._do_search(
            on_done=lambda tracks: self._auto_play_identify_results(generation, tracks),
            on_failed=lambda err: self._on_identify_search_failed(generation, err),
        )

    def _invalidate_identify_auto_play(self):
        self._identify_auto_generation += 1

    def _on_identify_search_failed(self, generation, err):
        if generation != self._identify_auto_generation:
            return
        self.lbl_status.setText(f"识曲成功，但在线音源搜索失败：{err}")

    def _auto_play_identify_results(self, generation, tracks=None):
        """搜索完成后按顺序尝试音源；旧识曲任务不能触发播放。"""
        if generation != self._identify_auto_generation:
            return
        tracks = list(self._tracks_by_row if tracks is None else tracks)
        if not tracks:
            self.lbl_status.setText("歌曲已识别，但没有找到可用在线音源")
            return
        self.lbl_status.setText("歌曲已识别，正在尝试播放可用音源…")

        def run():
            for track in tracks:
                if generation != self._identify_auto_generation:
                    return
                try:
                    if self._music is not None and self._music.play_online(track):
                        self._pending_identify_close = (generation, track.title, track.artist)
                        self._play_result.emit(True, f"正在播放：{track.title}", self.lbl_status)
                        return
                except Exception:
                    continue
            if generation == self._identify_auto_generation:
                self._play_result.emit(False, "歌曲已识别，但可用音源均无法播放", self.lbl_status)

        threading.Thread(target=run, daemon=True, name="ui-identify-auto-play").start()

    def _finish_identify_playback(self, generation):
        pending = self._pending_identify_close
        if not pending or pending[0] != generation:
            return
        self._pending_identify_close = None
        dialog = self._identify_dialog
        if dialog is not None and dialog.isVisible():
            dialog.hide()
        if self.tabs.currentIndex() != 1:
            self.tabs.setCurrentIndex(1)

    def _on_identify_play(self, result):
        """识别成功 → 本地曲库 → play_song；保留旧接口的未知结果。"""
        music = self._music
        if music is None:
            return False
        filepath = (result or {}).get("filepath") or ""
        if not filepath:
            return False
        try:
            song = music.db.get_song_by_path(filepath)
            if not song:
                return False
            return music.play_song(song)
        except Exception as e:
            print(f"[Identify] 播放失败: {type(e).__name__}")
            return False

    def _refresh_history(self):
        """渲染播放历史（最近在前）。"""
        if self._music is None:
            return
        try:
            rows = self._music.get_history(limit=200)
        except Exception as e:
            print(f"[MusicPanel] get_history error: {e}")
            rows = []
        self.list_history.clear()
        for i, row in enumerate(rows):
            title = row.get("title", "?") or "?"
            artist = row.get("artist", "") or ""
            st = row.get("source_type", "") or ""
            sn = row.get("source_name", "") or ""
            # 封面补全兜底：controller 已反查 path/cover_url；此处再兜底并打日志
            if self._music is not None and hasattr(self._music, "lookup_cover"):
                try:
                    row = self._music.lookup_cover(row)
                except Exception:
                    pass
            if i < 3:
                print(f"[MusicPanel] history item#{i}: "
                      f"title={title!r} artist={artist!r} source_type={st!r} "
                      f"music_id={row.get('music_id')!r} "
                      f"cover_url={row.get('cover_url')!r} path={row.get('path')!r}")
            if st == "local":
                src = "本地"
            elif sn:
                src = sn
            else:
                src = st or "在线"
            rel = self._fmt_relative(float(row.get("played_at") or 0))
            item = QListWidgetItem(f"{title} - {artist} [{src}] {rel}")
            mid = row.get("music_id") or row.get("id")
            item.setData(Qt.UserRole, self._card_data(
                row,
                f"{artist} · {rel}" if artist else rel,
                f"local:{mid}" if mid else None))
            self.list_history.addItem(item)

    @staticmethod
    def _fmt_relative(ts: float) -> str:
        """相对时间：刚刚 / N 分钟前 / N 小时前 / 昨天 / N 天前。"""
        import time as _t
        d = _t.time() - ts
        if d < 60:
            return "刚刚"
        if d < 3600:
            return f"{int(d // 60)} 分钟前"
        if d < 86400:
            return f"{int(d // 3600)} 小时前"
        if d < 172800:
            return "昨天"
        return f"{int(d // 86400)} 天前"

    def _on_history_play(self, item: QListWidgetItem):
        self._play_saved_row(self.list_history, item)

    def _on_clear_history(self):
        if self._music is None:
            return
        ret = QMessageBox.question(
            self, "清空播放历史", "确定清空全部播放历史？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        try:
            self._music.clear_history()
        except Exception as e:
            print(f"[MusicPanel] clear_history error: {e}")
        self._refresh_history()

    # ---- 歌单 ----

    def set_favorites_view(self, enabled):
        if enabled == self._favorites_only:
            return
        selected = self.list_playlists.currentItem()
        if enabled:
            self._browse_playlist_id = selected.data(Qt.UserRole)["id"] if selected else None
        self._favorites_only = enabled
        self.playlist_collection.setVisible(not enabled)
        self._update_heading()
        if self.tabs.currentIndex() == 3:
            self._refresh_playlists(preferred_id=None if enabled else self._browse_playlist_id)

    def _refresh_visible_playlists(self):
        if self.tabs.currentIndex() == 3:
            self._refresh_playlists()

    def _refresh_playlists(self, *, poll=False, preferred_id=None):
        """Read playlist data off the GUI thread and apply the requested selection."""
        if self._music is None or (poll and self._playlist_task._running):
            return
        listing = self.list_playlists
        selected = listing.currentItem()
        current = preferred_id
        if current is None and selected is not None:
            current = selected.data(Qt.UserRole)["id"]
        displayed = [listing.item(i).data(Qt.UserRole) for i in range(listing.count())]
        music = self._music
        favorites_only = self._favorites_only

        def read():
            from ...music.database import _LIKED_PLAYLIST
            playlists = music.get_playlists()
            if favorites_only:
                playlists = [row for row in playlists if row.get("name") == _LIKED_PLAYLIST]
            if poll and playlists == displayed:
                return None
            selected_id = next((p["id"] for p in playlists if p["id"] == current),
                               playlists[0]["id"] if playlists else None)
            songs = music.get_playlist_songs(selected_id) if selected_id is not None else []
            return playlists, selected_id, songs

        self._playlist_task.submit(None, read)

    def _apply_playlists(self, _key, snapshot, error):
        if error is not None or snapshot is None:
            return
        playlists, selected_id, rows = snapshot
        listing = self.list_playlists
        displayed = [listing.item(i).data(Qt.UserRole) for i in range(listing.count())]
        with QSignalBlocker(listing):
            if playlists != displayed:
                listing.clear()
                for playlist in playlists:
                    item = QListWidgetItem(playlist.get("name", "?"))
                    item.setData(Qt.UserRole, playlist)
                    listing.addItem(item)
            selected_row = next((i for i, playlist in enumerate(playlists)
                                 if playlist["id"] == selected_id), -1)
            listing.setCurrentRow(selected_row)
        self.list_playlist_songs.clear()
        for row in rows:
            title = row.get("title", "?") or "?"
            artist = row.get("artist", "") or ""
            st = row.get("source_type", "") or ""
            sn = row.get("source_name", "") or ""
            src = "本地" if st == "local" else (sn or "在线")
            item = QListWidgetItem(f"{title} - {artist} [{src}]")
            mid = row.get("music_id") or row.get("id")
            item.setData(Qt.UserRole, self._card_data(
                row,
                f"{artist} · {src}" if artist else src,
                f"local:{mid}" if mid else None))
            self.list_playlist_songs.addItem(item)
        selected = listing.currentItem()
        self.playlist_title.setText(selected.text() if selected else ("我喜欢的音乐" if self._favorites_only else "歌单"))
        cover = next((row for row in rows if row.get("cover_url") or row.get("path")), {})
        self.playlist_header.set_song(cover)
        self.playlist_art.set_song(cover)
        self.play_playlist_button.setVisible(bool(rows))
        self.play_playlist_button.setEnabled(bool(rows))
        if self._favorites_only and not rows:
            self.lbl_playlist_status.setText("尚未收藏歌曲")
        else:
            self.lbl_playlist_status.setText(f"{len(rows)} 首" if selected_id is not None else "未选择歌单")

    def _refresh_playlist_songs(self):
        self._refresh_playlists()

    def _on_playlist_selected(self, current, previous):
        """歌单列表选中变化 → 刷新右侧歌曲。"""
        self.list_playlist_songs.clear()
        self.play_playlist_button.hide()
        self.lbl_playlist_status.setText("正在读取歌单…" if current is not None else "未选择歌单")
        self._refresh_playlist_songs()

    def _on_new_playlist(self):
        if self._music is None:
            return
        name, ok = QInputDialog.getText(self, "新建歌单", "歌单名称：")
        if not ok or not name.strip():
            return
        try:
            pid = self._music.create_playlist(name.strip())
        except Exception as e:
            print(f"[MusicPanel] create_playlist error: {e}")
            pid = None
        if pid is None:
            self.lbl_playlist_status.setText("歌单名已存在")
            return
        self._refresh_playlists()
        self.lbl_playlist_status.setText("歌单已创建")

    def _on_rename_playlist(self):
        it = self.list_playlists.currentItem()
        if it is None or self._music is None:
            return
        p = it.data(Qt.UserRole)
        if not p:
            return
        name, ok = QInputDialog.getText(
            self, "重命名歌单", "新名称：", text=p.get("name", ""))
        if not ok or not name.strip() or name.strip() == p.get("name"):
            return
        try:
            ok2 = self._music.rename_playlist(p.get("id"), name.strip())
        except Exception as e:
            print(f"[MusicPanel] rename_playlist error: {e}")
            ok2 = False
        if not ok2:
            self.lbl_playlist_status.setText("歌单名已存在")
            return
        self._refresh_playlists()
        self.lbl_playlist_status.setText("已重命名")

    def _on_delete_playlist(self):
        it = self.list_playlists.currentItem()
        if it is None or self._music is None:
            return
        p = it.data(Qt.UserRole)
        if not p:
            return
        n = self.list_playlist_songs.count()
        ret = QMessageBox.question(
            self, "删除歌单",
            f"确定删除歌单「{p.get('name')}」？\n（{n} 首歌将一并移除，此操作不可逆）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self._music.delete_playlist(p.get("id"))
        except Exception as e:
            print(f"[MusicPanel] delete_playlist error: {e}")
        self._refresh_playlists()
        self.lbl_playlist_status.setText("歌单已删除")

    def _on_playlist_song_play(self, item: QListWidgetItem):
        self._play_saved_row(self.list_playlist_songs, item)

    def _play_saved_row(self, listing, item):
        row = item.data(Qt.UserRole)
        if not row or self._music is None:
            return
        index = listing.row(item)
        rows = (tuple(listing.item(i).data(Qt.UserRole) for i in range(listing.count()))
                if index >= 0 else (row,))
        self._play_async(self._music.play_playlist_context, rows, max(0, index),
                         title=row.get("title", ""), context=True,
                         fail_msg="该歌已不可用")

    # ---- 榜单 ----

    def _load_top_lists(self):
        """切到榜单 tab → 后台拉全部插件榜单分组（防 UI 卡；失败降级提示不崩）。

        防重入：上一轮分组线程仍在运行 → 记待处理（done/failed 后自动补拉），
        绝不新建线程覆盖运行中引用（QThread 崩溃修复）。
        """
        if self._music is None:
            return
        if self._rank_thread is not None and self._rank_thread.isRunning():
            self._pending_rank_lists = True
            return
        self._pending_rank_lists = False
        self._rank_detail_serial += 1
        self._pending_rank_detail = None
        self.lbl_rank_status.setText("加载榜单中…")
        self._rank_groups = {}
        self.list_rank_sources.clear()
        self.list_rank_lists.clear()
        self.list_rank_songs.clear()
        self._rank_tracks = []
        try:
            self._rank_thread = QThread()
            self._rank_worker = _RankListsWorker(self._music)
            self._rank_worker.moveToThread(self._rank_thread)
            self._rank_thread.started.connect(self._rank_worker.run)
            self._rank_worker.done.connect(self._on_rank_lists_done)
            self._rank_worker.failed.connect(self._on_rank_lists_failed)
            self._rank_worker.done.connect(self._rank_thread.quit)
            self._rank_worker.failed.connect(self._rank_thread.quit)
            self._rank_thread.finished.connect(self._resume_rank_lists)
            self._rank_thread.start()
        except Exception as e:
            self.lbl_rank_status.setText(f"榜单加载失败: {e}")

    def _on_rank_lists_done(self, groups: dict):
        if self._pending_rank_lists:
            return
        self._rank_groups = groups or {}
        plugins = list(self._rank_groups.keys())
        self.list_rank_sources.clear()
        self.list_rank_lists.clear()
        self.list_rank_songs.clear()
        self._rank_tracks = []
        for p in plugins:
            item = QListWidgetItem(p)
            item.setData(Qt.UserRole, p)
            self.list_rank_sources.addItem(item)
        if not plugins:
            self.lbl_rank_status.setText("暂无平台提供榜单（插件无榜单能力或加载失败）")
        else:
            self.list_rank_sources.setCurrentRow(0)
        errors = getattr(groups, "errors", {})
        if errors:
            text = (f"已加载 {len(plugins)} 个平台，部分音源未完成；可重试或更换音源" if plugins else
                    "榜单未能加载，请检查网络、重试或更换音源")
            self.lbl_rank_status.setText(text)
            notify("音乐榜单部分可用" if plugins else "音乐榜单未完成", text,
                   source="music", status="warning" if plugins else "failed")
        elif plugins:
            notify("音乐榜单已加载", f"{len(plugins)} 个平台提供真实榜单，可选择查看", source="music")
        # A pending refresh resumes only from QThread.finished, after quit.

    @Slot()
    def _resume_rank_lists(self):
        if self._pending_rank_lists:
            self._pending_rank_lists = False
            self._load_top_lists()

    def _on_rank_lists_failed(self, err: str):
        if not self._pending_rank_lists:
            self.lbl_rank_status.setText("榜单加载未完成，请重试或更换音源。")
            notify("音乐榜单未完成", self.lbl_rank_status.text(), source="music", status="failed")

    def _on_rank_source_selected(self, current, previous):
        """平台选中变化 → 下方榜单列表（该平台全部分组的榜单）。"""
        if current is None:
            return
        self._rank_detail_serial += 1
        self._pending_rank_detail = None
        plugin = current.data(Qt.UserRole)
        groups = self._rank_groups.get(plugin) or []
        self.list_rank_lists.clear()
        self.list_rank_songs.clear()
        self._rank_tracks = []
        for g in groups:
            gtitle = (g.get("title") or "").strip()
            for lst in g.get("lists", []):
                ltitle = (lst.get("title") or "").strip()
                label = f"{gtitle} · {ltitle}" if gtitle and ltitle and gtitle != ltitle else (ltitle or gtitle or "?")
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, {
                    "source": plugin,
                    "list_id": lst.get("id", ""),
                    "title": ltitle or gtitle or "?",
                })
                self.list_rank_lists.addItem(item)
        if self.list_rank_lists.count():
            self.list_rank_lists.setCurrentRow(0)
        else:
            self.lbl_rank_status.setText("该平台无榜单")

    def _on_rank_list_selected(self, current, previous):
        """榜单选中变化 → 后台拉该榜单歌曲。"""
        if current is None:
            return
        meta = current.data(Qt.UserRole)
        if not meta or not meta.get("list_id"):
            return
        self._load_rank_detail(meta["source"], meta["list_id"], meta.get("title", ""))

    def _load_rank_detail(self, source: str, list_id: str, title: str):
        if self._music is None:
            return
        self._rank_detail_serial += 1
        # 防重入：上一轮详情线程仍在运行 → 记最新请求（覆盖旧的），
        # 旧线程 done/failed 后自动补加载。绝不覆盖运行中线程引用（崩溃修复）。
        if self._rank_detail_thread is not None and self._rank_detail_thread.isRunning():
            self._pending_rank_detail = {"source": source, "list_id": list_id, "title": title}
            self.lbl_rank_status.setText(f"加载「{title}」…")
            return
        self._pending_rank_detail = None
        self._rank_active_serial = self._rank_detail_serial
        self.lbl_rank_status.setText(f"加载「{title}」…")
        self.list_rank_songs.clear()
        self._rank_tracks = []
        try:
            self._rank_detail_thread = QThread()
            self._rank_detail_worker = _RankDetailWorker(self._music, source, list_id)
            self._rank_detail_worker.moveToThread(self._rank_detail_thread)
            self._rank_detail_thread.started.connect(self._rank_detail_worker.run)
            self._rank_detail_worker.done.connect(self._on_rank_detail_done)
            self._rank_detail_worker.failed.connect(self._on_rank_detail_failed)
            self._rank_detail_worker.done.connect(self._rank_detail_thread.quit)
            self._rank_detail_worker.failed.connect(self._rank_detail_thread.quit)
            self._rank_detail_thread.finished.connect(self._resume_rank_detail)
            self._rank_detail_thread.start()
        except Exception as e:
            self.lbl_rank_status.setText(f"榜单详情加载失败: {e}")

    def _on_rank_detail_done(self, payload: dict):
        if self._rank_active_serial != self._rank_detail_serial:
            return
        tracks = (payload or {}).get("tracks") or []
        self._rank_tracks = tracks
        if self._music is not None:
            self._music.set_voice_rank_tracks(tracks)
        self.list_rank_songs.clear()
        for i, t in enumerate(tracks, 1):
            text = f"{i:>2}. {t.title}"
            if t.artist:
                text += f" - {t.artist}"
            item = QListWidgetItem(text)
            # extra 必须随卡携带（含 _raw 原始歌曲对象）：play_online → getUrl
            # 依赖 raw_item 构造插件 URL，缺了会 AttributeError → 榜单永远无法播放。
            if self._music is not None and hasattr(self._music, "remember_cover"):
                try:
                    self._music.remember_cover(
                        title=t.title or "", artist=t.artist or "",
                        source_name=t.source_name or "",
                        track_id=t.track_id or "",
                        cover_url=getattr(t, "cover_url", None) or "")
                except Exception:
                    pass
            item.setData(Qt.UserRole, self._card_data({
                "title": t.title,
                "artist": t.artist,
                "source_name": t.source_name,
                "track_id": t.track_id,
                "cover_url": getattr(t, "cover_url", None) or None,
                "extra": t.extra,
                "duration_seconds": t.duration_seconds,
                "album": t.album,
            }, " · ".join(v for v in (t.artist, t.album, t.source_name) if v), f"net:{t.track_id}" if t.track_id else None))
            self.list_rank_songs.addItem(item)
        n = len(tracks)
        self.lbl_rank_status.setText(f"共 {n} 首，双击播放" if n else "榜单无歌曲")
        # A newer selection resumes from QThread.finished, never a zero-delay poll.

    @Slot()
    def _resume_rank_detail(self):
        if self._pending_rank_detail:
            req = self._pending_rank_detail
            self._pending_rank_detail = None
            self._load_rank_detail(req["source"], req["list_id"], req["title"])

    def _on_rank_detail_failed(self, err: str):
        if self._rank_active_serial == self._rank_detail_serial:
            self.lbl_rank_status.setText("榜单详情未完成，请重新选择或换一个音源。")
            notify("榜单曲目未完成", self.lbl_rank_status.text(), source="music", status="failed")

    def _on_rank_song_play(self, item: QListWidgetItem):
        data = item.data(Qt.UserRole)
        if not data or self._music is None:
            return
        # 卡片 UserRole 存的是 dict（Delegate 用）；play_online 依赖 OnlineTrack 属性。
        # 必须还原为 OnlineTrack 对象，且 extra 完整携带（含 _raw 原始歌曲对象，getUrl 依赖）。
        if isinstance(data, OnlineTrack):
            track = data
        else:
            track = OnlineTrack(
                source_name=data.get("source_name", "") or "",
                track_id=data.get("track_id", "") or "",
                title=data.get("title", "") or "",
                artist=data.get("artist", "") or "",
                album=data.get("album", "") or "",
                duration_seconds=int(data.get("duration_seconds") or 0),
                cover_url=data.get("cover_url") or None,
                extra=data.get("extra") or {},
            )
        idx = self.list_rank_songs.row(item)
        if idx < 0 or idx >= len(self._rank_tracks):
            return
        self._play_async(self._music.play_online_list, self._rank_tracks, idx,
                         title=track.title, resolving=True, context=True,
                         fail_msg="该曲目无法播放",
                         status_label=self.lbl_rank_status)

    # ---- 右键菜单 ----

    @staticmethod
    def _song_dict_from_row(row) -> dict:
        """列表行 → 加入歌单/插队用的 song dict（在线歌不存 URL，只存源名+track_id）。
        id 统一取 music_id（队列播放链路 song['id'] 是 music.id，历史/歌单行的 row.id 不是）。"""
        st = row.get("source_type", "local")
        mid = int(row.get("music_id") or row.get("id") or 0)
        return {
            "title": row.get("title", ""),
            "artist": row.get("artist", ""),
            "source_type": st,
            "source_name": row.get("source_name", "") if st != "local" else "",
            "track_id": row.get("track_id", "") if st != "local" else "",
            "music_id": mid,
            "id": mid,
            "path": row.get("path") or "",
            "filename": row.get("filename", ""),
        }

    def _populate_playlist_submenu(self, sub, song: dict):
        """「添加到歌单 ▸」子菜单：列出全部歌单 + 新建。"""
        try:
            pls = self._music.get_playlists()
        except Exception as e:
            print(f"[MusicPanel] get_playlists error: {e}")
            pls = []
        for p in pls:
            pid = p.get("id")
            act = sub.addAction(p.get("name", "?"))
            act.triggered.connect(
                lambda _=False, pid=pid: self._add_to_playlist(pid, song))
        if pls:
            sub.addSeparator()
        act_new = sub.addAction("新建歌单…")
        act_new.triggered.connect(lambda: self._add_to_new_playlist(song))

    def _add_to_playlist(self, playlist_id, song: dict):
        if self._music is None:
            return
        # 统一规范化：本地列表项是 music 表原始 dict（无 source_type 键），
        # 历史/歌单行是数据库行——都转成标准 song dict，保证 source_type/music_id 正确
        song = self._song_dict_from_row(song)
        try:
            ok = self._music.add_to_playlist(playlist_id, song)
        except Exception as e:
            print(f"[MusicPanel] add_to_playlist error: {e}")
            ok = False
        if ok:
            self.lbl_status.setText("已添加到歌单")
        else:
            self.lbl_status.setText("该歌已在歌单中")
        if self.tabs.currentIndex() == 3:
            self._refresh_playlists()

    def _add_to_new_playlist(self, song: dict):
        if self._music is None:
            return
        name, ok = QInputDialog.getText(self, "添加到新歌单", "歌单名称：")
        if not ok or not name.strip():
            return
        try:
            pid = self._music.create_playlist(name.strip())
        except Exception as e:
            print(f"[MusicPanel] create_playlist error: {e}")
            pid = None
        if pid is None:
            self.lbl_status.setText("歌单名已存在")
            return
        self._add_to_playlist(pid, song)

    def _build_song_menu(self, song: dict, playlist_song_id=None):
        """通用右键菜单；parent=self 防 GC。song 须含 title/artist/source_type/
        source_name/track_id/music_id（本地可带 path）。"""
        menu = QMenu(self)
        from ...music.database import liked_song_identity
        row_song = self._song_dict_from_row(song)
        identity = liked_song_identity(row_song)
        if identity is not None and row_song["title"] and self._music is not None:
            favorite = menu.addAction("收藏到我喜欢的音乐")
            favorite.triggered.connect(lambda: self._favorite_row(identity, row_song))
        act_next = menu.addAction("下一首播放")
        act_next.triggered.connect(lambda: self._on_next_play(song))
        act_append = menu.addAction("添加到播放队列")
        act_append.triggered.connect(lambda: self._on_append_queue(song))
        sub = menu.addMenu("添加到歌单 ▸")
        self._populate_playlist_submenu(sub, song)
        if playlist_song_id is not None:
            act_rm = menu.addAction("从歌单内删除")
            act_rm.triggered.connect(
                lambda: self._remove_song_from_playlist(playlist_song_id))
        is_online = song.get("source_type", "local") != "local"
        if is_online:
            try:
                downloaded = self._music is not None and bool(
                    "_download_alternatives" not in song and
                    self._music.is_downloaded(song.get("title", ""), song.get("artist", "")))
            except Exception:
                downloaded = False
            if downloaded:
                act_del_dl = menu.addAction("删除本地下载")
                act_del_dl.triggered.connect(lambda: self._delete_download(song))
            else:
                act_dl = menu.addAction("一键最佳下载")
                act_dl.triggered.connect(lambda: self._on_download_song(song))
                menu.addAction("自定义下载…").triggered.connect(lambda:self._custom_download(song))
        is_local = song.get("source_type", "local") == "local"
        if is_local:
            path = song.get("path") or ""
            if not path and int(song.get("music_id") or 0) > 0:
                try:
                    path = self._music.get_song_path(song["music_id"])
                except Exception:
                    path = ""
            if path:
                act_dir = menu.addAction("打开所在文件夹")
                act_dir.triggered.connect(lambda: self._open_song_folder(path))
            act_del = menu.addAction("删除本地文件")
            act_del.triggered.connect(lambda: self._delete_local_file(song))
        return menu

    def _favorite_row(self, identity, song):
        if identity in self._row_favorites:
            return
        task = LatestTask(self)
        self._row_favorites[identity] = task
        music = self._music

        def done(_key, saved, error):
            self._row_favorites.pop(identity, None)
            task.deleteLater()
            if error is None and saved is True:
                self.lbl_status.setText("已收藏")
                self.btn_like.refresh_membership(song)
                self._refresh_visible_playlists()
            else:
                self.lbl_status.setText("收藏未更新，请重试")

        task.completed.connect(done)
        task.submit(identity, lambda: music.set_liked_song(song, True))

    def _on_next_play(self, song: dict):
        if self._music is None:
            return
        try:
            d = self._song_dict_from_row(song)
            # 本地歌必须带磁盘路径才能入队播放（历史/歌单行无 path，按 music_id 补）
            if d.get("source_type") == "local":
                if not d.get("path"):
                    try:
                        d["path"] = self._music.get_song_path(d.get("music_id") or 0)
                    except Exception:
                        d["path"] = ""
                if not d["path"]:
                    self.lbl_status.setText("该歌已不可用")
                    return
            result = self._music.add_next(d)
        except Exception as e:
            print(f"[MusicPanel] add_next error: {e}")
            self.lbl_status.setText("加入下一首失败，请稍后重试")
            notify("未能设为下一首播放", "请重试或重新选择歌曲", status="failed", source="music")
            return
        if result is True:
            self.lbl_status.setText(f"已插到下一首：{song.get('title', '')}")
        elif result is False:
            self.lbl_status.setText("加入下一首失败，请重试")
        else:
            self.lbl_status.setText("加入下一首结果未确认")
        notify("已设为下一首播放" if result is True else "下一首设置结果",
               self.lbl_status.text(), status="success" if result is True else
               "failed" if result is False else "warning", source="music")

    def _on_append_queue(self, song: dict):
        if self._music is None:
            self.lbl_status.setText("音乐服务不可用，未添加到播放队列")
            notify("未能添加到播放队列", "音乐功能尚未就绪，请稍后再试", status="failed", source="music")
            return
        try:
            data = self._song_dict_from_row(song)
            if data["source_type"] == "local":
                if not data["path"]:
                    data["path"] = self._music.get_song_path(data.get("music_id") or 0)
                if not data["path"]:
                    self.lbl_status.setText("该歌已不可用，未添加到播放队列")
                    notify("未能添加到播放队列", "歌曲文件已不可用，请重新选择歌曲", status="failed", source="music")
                    return
            else:
                data["extra"] = song.get("extra") or {}
            result = self._music.add_to_queue(data)
        except Exception:
            logger.exception("添加到播放队列失败")
            result = False
        if not isinstance(result, OperationResult):
            result = media_result(result, label="歌曲", action="加入播放队列")
        self.lbl_status.setText(result.message)
        notify("已添加到播放队列" if result.status is OperationStatus.CONFIRMED else "歌曲入队结果",
               result.message, status="success" if result.status is OperationStatus.CONFIRMED else
               "failed" if result.status is OperationStatus.FAILED else "warning", source="music")

    def _remove_song_from_playlist(self, playlist_song_id):
        it = self.list_playlists.currentItem()
        if it is None or self._music is None:
            return
        p = it.data(Qt.UserRole)
        if not p:
            return
        try:
            self._music.remove_from_playlist(p.get("id"), playlist_song_id)
        except Exception as e:
            print(f"[MusicPanel] remove_from_playlist error: {e}")
        self._refresh_playlist_songs()

    def _open_song_folder(self, path):
        import os
        import subprocess
        try:
            # explorer 只吃 cmd shell 形态（subprocess list 模式标准 argv 解析会失效，
            # /select 路径无效 → 打开"文档"默认位置）；% → %% 防 cmd 变量展开
            p = os.path.normpath(path).replace("%", "%%")
            subprocess.Popen(f'explorer /select,"{p}"', shell=True)
        except Exception as e:
            self.lbl_status.setText(f"打开文件夹失败: {e}")

    def _delete_local_file(self, song: dict):
        """删除本地文件（不可逆）：二次确认默认取消 → controller 删磁盘+库。"""
        music_id = int(song.get("music_id") or song.get("id") or 0)
        path = song.get("path") or ""
        if not path and music_id > 0:
            try:
                path = self._music.get_song_path(music_id)
            except Exception:
                path = ""
        if not path:
            self.lbl_status.setText("歌曲文件不存在")
            return
        ret = QMessageBox.warning(
            self, "删除本地文件",
            f"确定要删除文件？\n{path}\n\n此操作不可逆！",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if ret != QMessageBox.StandardButton.Ok:
            return
        try:
            ok, err = self._music.delete_local_file(song)
        except Exception as e:
            self.lbl_status.setText(f"删除失败: {e}")
            return
        if ok:
            self.lbl_status.setText("已删除本地文件")
            self._refresh_local_list(self.edit_local.text().strip())
            if self.tabs.currentIndex() == 3:
                self._refresh_playlists()
        else:
            self.lbl_status.setText(f"删除失败: {err}")

    def _on_online_context_menu(self, pos):
        item = self.list_results.itemAt(pos)
        if item is None:
            return
        idx = self.list_results.row(item)
        if idx < 0 or idx >= len(self._tracks_by_row):
            return
        menu = self._build_song_menu(self._download_result_song(idx))
        menu.exec_(self.list_results.mapToGlobal(pos))

    def _download_result_song(self, row):
        track = self._tracks_by_row[row]
        alternatives = self._track_versions[row] if row < len(self._track_versions) else ()
        return self._online_song_row(track, alternatives)

    @staticmethod
    def _online_song_row(track, alternatives):
        return dict(title=track.title, artist=track.artist, album=track.album,
                    duration_seconds=track.duration_seconds, source_type="online",
                    source_name=track.source_name, track_id=track.track_id,
                    cover_url=track.cover_url, extra=dict(track.extra or {}),
                    _download_alternatives=tuple(alternatives), path="", music_id=0, id=0)

    @Slot(int)
    def _download_result(self, row):
        if self._voice_generation is None and 0 <= row < len(self._tracks_by_row):
            self._on_download_song(self._download_result_song(row))

    def _custom_download(self, song):
        from ..dialogs.download_options_dialog import DownloadOptionsDialog
        downloader = getattr(self._music, "downloader", None)
        verified = downloader.verified_options(song) if downloader is not None else ()
        probe = None
        if downloader is not None and song.get("source_name") and song.get("track_id"):
            probe = lambda ready, cancelled: downloader.probe_options(song, ready, cancelled)
        dialog = DownloadOptionsDialog(self, song=song, verified=verified, probe=probe)
        if dialog.exec() == dialog.Accepted:
            selected = dict(song)
            if song.get("source_name") and song.get("track_id"):
                selected["_download_alternatives"] = ()
            self._on_download_song(selected, options=dialog.options())
        dialog.deleteLater()

    @staticmethod
    def _download_task_key(song):
        source, track_id = song.get("source_name"), song.get("track_id")
        if source and track_id:
            return repr((source, str(track_id)))
        return repr((song.get("title"), song.get("artist")))

    def _show_download_tasks(self):
        if self._download_pane is None:
            from ..dialogs.download_tasks_dialog import DownloadTasksPane
            self._download_pane = DownloadTasksPane(self._media_pane)
            self._download_pane.close_requested.connect(self._close_media_pane)
            self._media_pane.addWidget(self._download_pane)
            self._download_pane.retry_requested.connect(self._retry_download)
            for key, task in self._download_tasks.items():
                self._download_pane.update_task(key, task)
        self._open_media_pane(self._download_pane)

    @Slot(str)
    def _retry_download(self, key):
        task = self._download_tasks.get(key)
        if task and task["state"] == "fail":
            self._on_download_song(task["song"], options=task["options"])

    def _on_download_song(self, song, *, options=None):
        from ...music.downloader import DownloadManager

        if self._music is None:
            return
        key = self._download_task_key(song)
        name = song.get("title") or "歌曲"
        filename_key = DownloadManager._key(song.get("title"), song.get("artist"))
        for existing in self._download_tasks.values():
            if existing["filename_key"] == filename_key and existing["state"] == "active":
                self._show_download_tasks()
                return

        current = self._download_tasks.get(key)
        if current and current["state"] == "done" and current["options"] == options:
            from pathlib import Path
            if current.get("path") and Path(current["path"]).is_file():
                self._show_download_tasks()
                self._download_pane.items.setCurrentItem(self._download_pane.rows[key])
                return
        self._download_serial += 1
        serial = self._download_serial
        notice = get_notification_center().post("正在下载", name, source="music")
        self._download_tasks[key] = dict(serial=serial, notice=notice, song=dict(song),
                                         options=None if options is None else dict(options),
                                         state="active", message="等待下载", filename_key=filename_key,
                                         search_generation=self._search_generation)
        self._render_download_task(key)
        self.lbl_status.setText(f"正在下载：{name}")

        def send(event, value=None):
            try:
                self._download_event.emit(key, serial, event, value)
            except RuntimeError:
                # The application can close while the downloader unwinds.
                pass

        callbacks = {
            "on_progress": lambda k, pct: send("progress", pct),
            "on_stage": lambda k, stage: send("stage", stage),
            "on_details": lambda k, details: send("details", details),
            "on_done": lambda k, path: send("done", path),
            "on_fail": lambda k, reason: send("fail", reason),
            "on_exists": lambda k: send("exists"),
            "on_cancel": lambda k, reason: send("cancelled"),
        }
        kwargs = {"options": options} if options is not None else {}
        if self._music.download_song(song, callbacks, **kwargs) is False:
            send("fail", "下载暂不可用")

    @Slot(str, int, str, object)
    def _apply_download_event(self, key, serial, event, value):
        task = self._download_tasks.get(key)
        if not task or task["serial"] != serial or task["state"] != "active":
            return
        if event == "details":
            for field in ("format", "quality", "size_bytes", "lyrics", "cover", "metadata", "indexed"):
                if field in value:
                    task[field] = value[field]
            return
        if event in ("progress", "stage"):
            task["message"] = (value if event == "stage" else
                               "正在下载" if value is None else f"当前文件 {value}%")
            self._render_download_task(key)
            return

        task["state"] = event
        status = "warning"
        if event == "done":
            status = "success"
            task["path"] = value
            message = "下载完成"
            missing = [name for field, name in (("lyrics", "歌词"), ("cover", "封面"), ("metadata", "歌曲信息"))
                       if task.get(field) == "unavailable"]
            if missing:
                message += "，" + "、".join(missing) + "暂缺"
            if task.get("indexed") is False:
                message += "，曲库尚未更新"
        elif event == "fail":
            status = "failed"
            if value in ("下载目录空间不足", "无法写入下载目录"):
                message = value + "，请检查下载目录后重试"
            else:
                reasons = {"所选音质或格式暂不可用", "来源暂未提供可下载音频", "音频未能完整下载", "下载暂不可用"}
                message = (value if value in reasons else "暂时无法下载") + "，可重试或选择其他版本"
        elif event == "exists":
            message = "已有同名本地文件，未写入"
        else:
            message = "下载已取消"

        task["message"] = message
        self._render_download_task(key)
        from ..dialogs.download_tasks_dialog import download_label
        if task["search_generation"] == self._search_generation:
            self.lbl_status.setText(download_label(task))
        get_notification_center().update(task["notice"], title=message, message=task["song"].get("title", ""),
                                         status=status, phase="done")
        if event == "done":
            self._refresh_local_list(self.edit_local.text().strip())
            if self.tabs.currentIndex() == 3:
                self._refresh_playlists()
        while len(self._download_tasks) > 80:
            oldest = next((item for item, record in self._download_tasks.items()
                           if item != key and record["state"] != "active"), None)
            if oldest is None:
                break
            self._download_tasks.pop(oldest)
            if self._download_pane is not None:
                self._download_pane.forget(oldest)

    def _render_download_task(self, key):
        task = self._download_tasks[key]
        self._update_download_card(key, task)
        if self._download_pane is not None:
            self._download_pane.update_task(key, task)

    def _update_download_card(self, key, task):
        for i, track in enumerate(self._tracks_by_row):
            if self._download_task_key({"source_name": track.source_name, "track_id": track.track_id}) != key:
                continue
            item = self.list_results.item(i)
            if item is not None:
                active = task["state"] == "active"
                item.setData(MusicCardRoles.StatusRole, task["message"] if active else None)
            break

    def _delete_download(self, song: dict):
        """删除下载副本：二次确认默认取消 → controller 删文件+库；歌单条目保留（在线可重下）。"""
        if self._music is None:
            return
        path = ""
        try:
            dl = getattr(self._music, "downloader", None)
            if dl is not None:
                path = dl.find_download_path(song.get("title", ""), song.get("artist", ""))
        except Exception:
            pass
        ret = QMessageBox.warning(
            self, "删除本地下载",
            f"确定要删除下载的本地副本？\n{path}\n\n（歌单中的该歌保留，可重新下载）",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if ret != QMessageBox.StandardButton.Ok:
            return
        try:
            ok, _p, err = self._music.remove_download(song)
        except Exception as e:
            self.lbl_status.setText(f"删除失败: {e}")
            return
        if ok:
            self.lbl_status.setText("已删除本地下载")
            self._refresh_local_list(self.edit_local.text().strip())
            if self.tabs.currentIndex() == 3:
                self._refresh_playlists()
        else:
            self.lbl_status.setText(f"删除失败: {err}")

    def _on_local_context_menu(self, pos):
        item = self.list_local.itemAt(pos)
        if item is None:
            return
        s = item.data(Qt.UserRole)
        if not s:
            return
        menu = self._build_song_menu(s)
        menu.exec_(self.list_local.mapToGlobal(pos))

    def _on_history_context_menu(self, pos):
        item = self.list_history.itemAt(pos)
        if item is None:
            return
        row = item.data(Qt.UserRole)
        if not row:
            return
        song = self._song_dict_from_row(row)
        menu = self._build_song_menu(song)
        menu.exec_(self.list_history.mapToGlobal(pos))

    def _on_playlist_songs_context_menu(self, pos):
        item = self.list_playlist_songs.itemAt(pos)
        if item is None:
            return
        row = item.data(Qt.UserRole)
        if not row:
            return
        song = self._song_dict_from_row(row)
        menu = self._build_song_menu(song, playlist_song_id=row.get("id"))
        menu.exec_(self.list_playlist_songs.mapToGlobal(pos))

    def _on_status_msg(self, msg):
        """主线程槽：跨线程状态栏消息（仅主线程允许直接 setText）。"""
        self.lbl_status.setText(msg)

    def _on_error_event(self, event):
        """换源全部失败 -> 状态栏提示（不跳下一首）。跨线程事件 → 信号桥主线程刷新。"""
        try:
            msg = (event.data or {}).get("message", "在线播放失败")
            self._status_signal.emit(msg)
        except Exception as e:
            print(f"[MusicPanel] 错误事件处理失败: {e}")

    def _online_selection_changed(self, row):
        self.btn_result_play.setEnabled(row >= 0)
        self.btn_result_play.setVisible(row >= 0)
        groups = getattr(self, "_track_versions", [])
        has_alternatives = (self._voice_generation is None and 0 <= row < len(groups)
                            and len({(t.source_name, t.track_id) for t in groups[row]}) > 1)
        self.btn_versions.setEnabled(has_alternatives)
        self.btn_versions.setVisible(has_alternatives)

    def _choose_online_version(self):
        row = self.list_results.currentRow()
        if self._voice_generation is not None or not 0 <= row < len(self._track_versions):
            return
        menu = QMenu(self)
        for track in self._track_versions[row]:
            text = " · ".join(filter(None, (track.artist, track.album, track.source_name)))
            menu.addAction(text or track.title).triggered.connect(
                lambda _=False, t=track: self._start_online_track(t, ()))
        menu.exec_(self.btn_versions.mapToGlobal(self.btn_versions.rect().bottomLeft()))

    def _start_online_track(self, track, alternatives):
        active = self._retry_online_track
        if (getattr(self, "_manual_request", None) is not None and active is not None
                and (active[0].source_name, active[0].track_id) == (track.source_name, track.track_id)):
            return  # A second click on the same in-flight request is not a new load.
        self._retry_online_track = (track, tuple(alternatives))
        self.btn_retry_online.hide()
        self._play_async(self._music.play_online, track, title=track.title, resolving=True,
                         fail_msg=PLAY_RETRY,
                         play_options={"_alternatives": tuple(alternatives), "_single_context": True})

    def _retry_online(self):
        if self._retry_online_track is not None:
            self._start_online_track(*self._retry_online_track)
        else:
            self._request_search()

    def _on_play(self, item: QListWidgetItem):
        row = self.list_results.row(item)
        if row < 0:
            return
        # P1 候选换源：语音候选列表（_voice_generation 非 None）→ 换源续播（后台线程）
        if self._voice_generation is not None:
            self.lbl_status.setText("正在换源…")
            self._play_async(self._music.play_online_resume,
                             self._voice_generation, row,
                             fail_msg="换源失败", resolving=True)
            return
        # 手动搜索路径：后台线程播放（UI 不等待网络/播放器）
        if row >= len(self._tracks_by_row):
            return
        track = self._tracks_by_row[row]
        if self._discovery_tracks is not None:
            self._play_async(self._music.play_online_list, tuple(self._tracks_by_row), row,
                             title=track.title, resolving=True, context=True)
            return
        alternatives = self._track_versions[row] if row < len(self._track_versions) else [track]
        self._start_online_track(track, alternatives)

    def _on_seek_preview(self, value: int) -> None:
        """拖动进度条（sliderMoved，主线程）：只更新时间标签预览目标位置，
        不碰任何 mpv IPC（seek 指令仍由 sliderReleased → 后台线程执行）。"""
        try:
            dur = getattr(self, "_last_dur", 0.0)
            if dur <= 0:
                return
            frac = value / 1000.0
            t = min(max(0.0, frac * dur), dur)
            self.lbl_time.setText(
                f"{int(t//60)}:{int(t%60):02d} / {int(dur//60)}:{int(dur%60):02d}")
        except Exception:
            pass

    def _on_seek(self):
        """进度条拖拽：seek 整体丢后台线程执行——mpv IPC（get_duration/seek）绝不碰主线程。"""
        if self._music is None:
            return
        frac = self.slider_pos.value() / 1000.0
        threading.Thread(target=self._safe_call, args=(self._do_seek, frac),
                         daemon=True, name="ui-music-seek").start()

    def _on_chorus_click(self, marker):
        cancel = self._chorus_cancel
        worker = getattr(self, "_chorus_seek_thread", None)
        if worker is not None and worker.is_alive():
            return
        if not cancel.is_set() and self._music.chorus_is_current(marker):
            self._chorus_seek_thread = threading.Thread(
                target=self._safe_call, args=(self._music.seek_chorus, marker, cancel),
                daemon=True, name="ui-music-chorus-seek")
            self._chorus_seek_thread.start()

    def _do_seek(self, frac: float) -> None:
        """后台线程执行 seek（IPC 出主线程；失败静默，不炸线程）。
        统一走 controller 入口：享受 _is_seeking 防抖与快照同步——
        直连 mpv.seek 会绕过防抖，拖拽期间 mpv 短暂 idle 会被误判为播放结束而切歌。
        目标为绝对位置：frac * duration，必须显式 mode="absolute"
        （controller 默认 relative 是"当前位置 + seconds"语义）。"""
        try:
            dur = self._music.mpv.get_duration() if hasattr(self._music, "mpv") else 0
            if dur > 0:
                self._music.seek(frac * dur, mode="absolute")
        except Exception:
            pass

    def _set_meter_request(self, key):
        self._meter_request = (object(), key) if key else None
        self._meter_wake.set()

    @Slot(object, object, float, float)
    def _on_meter_sample(self, request, levels, stamp, position):
        if request is self._meter_request and abs(position - self._music.current_playback_time) < .65:
            self.playing_lyrics.stage.accept_levels(request[1], levels, stamp)

    @Slot(object)
    def _progress_finished(self, worker):
        if self._progress_thread is worker and self._progress_polling:
            self._progress_thread = None
            self._start_progress_poll()

    def _start_progress_poll(self):
        """后台线程每 400ms 读 mpv 进度（playback-time/duration 的 IPC 不碰主线程），
        经 _progress_signal 回主线程更新滑块/时间。面板隐藏时停轮询（线程退出后可重启）。"""
        th = getattr(self, "_progress_thread", None)
        if th is not None and th.is_alive():
            return

        def read_progress():
            try:
                if self._music is not None and hasattr(self._music, "mpv"):
                    # 读 controller 受控缓存（_poll_idle 内带代次/切歌闸门，
                    # 在线解析窗口内旧歌位置不会污染）——绝不直连 mpv 实时值。
                    visibility = self._chorus_cancel
                    pos = self._music.current_playback_time
                    observe = getattr(self._music, "observe_chorus", None)
                    observed = observe() if observe else None
                    dur = observed[1] if isinstance(observed, tuple) else self._music.mpv.get_duration()
                    self._progress_signal.emit(pos, dur, (visibility, observed))
                    volume_generation = self._volume_request_gen
                    volume = self._music.get_volume()
                    if not self._progress_polling:
                        return
                    if type(volume) in (int, float) and 0 <= volume <= 100:
                        self._volume_signal.emit(volume_generation, int(volume))
            except Exception:
                pass

        def poll():
            from ..widgets.lyrics_window import LyricsWindow
            meter = getattr(getattr(self._music, "mpv", None), "lyric_levels", None)
            metering = False
            next_progress = 0.
            try:
                while self._progress_polling:
                    if time.monotonic() >= next_progress:
                        read_progress()
                        next_progress = time.monotonic() + .4
                    request = self._meter_request
                    if request and callable(meter):
                        if (not getattr(self._music, "_is_switching_track", False)
                                and request[1] == LyricsWindow._song_key(self._music.get_current_song())):
                            metering = True
                            stamp = time.monotonic()
                            position = self._music.current_playback_time
                            levels = meter(True)
                            if request is self._meter_request and isinstance(levels, tuple):
                                self._meter_signal.emit(request, levels, stamp, position)
                    elif metering:
                        meter(False)
                        metering = False
                    delay = min(.085 if request else .4, max(0., next_progress - time.monotonic()))
                    self._meter_wake.wait(delay)
                    self._meter_wake.clear()
            finally:
                if metering:
                    meter(False)
                try:
                    self._poll_finished.emit(threading.current_thread())
                except RuntimeError:
                    pass  # Panel already destroyed during application shutdown.

        self._progress_thread = threading.Thread(target=poll, daemon=True,
                                                 name="ui-music-progress-poll")
        self._progress_thread.start()

    @Slot(int, int)
    def _on_volume_snapshot(self, generation, volume):
        if (not self._progress_polling or generation != self._volume_request_gen
                or self.slider_vol.isSliderDown()):
            return
        self._syncing_volume = True
        try:
            # Keep popover signals, but never write this observation back to MPV.
            self.slider_vol.setValue(volume)
        finally:
            self._syncing_volume = False

    def _on_progress(self, pos: float, dur: float, sample=None) -> None:
        """主线程槽：只做内存级 UI 更新（setValue/setText），不碰 mpv IPC。"""
        try:
            if sample is not None:
                visibility, observed = sample
                if visibility.is_set() or visibility is not self._chorus_cancel:
                    return
                if isinstance(observed, tuple) and self._music.chorus_is_current(observed[0]):
                    dur = observed[1]
                    self.slider_pos.set_chorus(observed)
                else:
                    self.slider_pos.set_chorus(None)
            self._last_dur = dur  # 供拖动预览换算目标时间（_on_seek_preview）
            # 切换态（controller._is_switching_track）：在线解析窗口内 mpv 仍报
            # 旧歌实时位置，禁止渲染——保持归零 + "加载中"，新歌 loadfile 起播
            # （controller 置 False）后恢复渲染。
            if getattr(self._music, "_is_switching_track", False):
                # 正在拖动时不强刷回零（拖动中的 setValue 由用户控制）
                if not self.slider_pos.isSliderDown():
                    self.slider_pos.setValue(0)
                self.lbl_time.setText("加载中...")
                return
            pending = self._pending_identify_close
            if pending and dur > 0 and pos >= 0:
                generation, title, artist = pending
                if generation == self._identify_auto_generation:
                    current = self._music.get_current_song() or {}
                    if ((current.get("title") or "") == title and
                            (current.get("artist") or "") == artist):
                        self._finish_identify_playback(generation)
            if dur > 0:
                self.slider_pos.setEnabled(True)
                if not self.slider_pos.isSliderDown():
                    self.slider_pos.setValue(int(pos / dur * 1000))
                self.lbl_time.setText(
                    f"{int(pos//60)}:{int(pos%60):02d} / {int(dur//60)}:{int(dur%60):02d}")
        except Exception:
            pass

    def _refresh(self):
        if self._music is None:
            return
        now = time.monotonic()
        previous = getattr(self, "_last_ui_tick", None)
        self._last_ui_tick = now
        if previous is not None and now - previous > 3:
            logging.getLogger(__name__).warning("gui_latency panel=music elapsed_ms=%.0f", (now - previous) * 1000)
        try:
            # 进度滑块/时间由后台轮询线程（_progress_signal → _on_progress）驱动，
            # 主线程绝不同步 IPC 读 mpv——playback-time/duration 响应慢曾造成拖拽窗口卡顿。
            song = self._music.get_current_song()
            if hasattr(self, "btn_like"):
                self.btn_like.sync()
            if not song:
                self.lbl_track.setText("未播放")
                self.lbl_artist.setText("")
                self.cover_thumb.set_song({})
                self.quality_button.refresh()
            if song:
                title = song.get("title") or song.get("name") or ""
                artist = song.get("artist") or ""
                self.lbl_track.setText(title)
                self.lbl_artist.setText(artist)
                self.cover_thumb.set_song(song)
                self.quality_button.refresh()
                # 播放/暂停图标随状态切换（定时器路径，复用同一同步方法）
                self._sync_toggle_icon()
                # 卡片播放态同步（仅 key 变化时更新全部卡片视图）
                pkey = self._play_key(song)
                if pkey != self._playing_card_key:
                    self._playing_card_key = pkey
                    for d in (self._dl_local, self._dl_results, self._dl_history,
                              self._dl_playlist_songs, self._dl_rank_songs):
                        d.playing_key = pkey
                    for w in (self.list_local, self.list_results, self.list_history,
                              self.list_playlist_songs, self.list_rank_songs):
                        w.viewport().update()
                # 播放态高亮只由 Delegate playing_key 驱动（不 setCurrentRow、不滚动，
                # 用户上下翻找列表时绝不被打断）
        except Exception as e:
            print(f"[UI] music refresh error: {e!r}")
        # 同步播放模式/高潮模式按钮：独立 try，不被上面的进度/歌曲异常拖累
        try:
            self._sync_state_buttons()
        except Exception as e:
            print(f"[UI] sync buttons error: {e!r}")

    def _sync_state_buttons(self):
        """Reflect backend playback state, including changes initiated by voice."""
        if self._music is None:
            return
        self._sync_play_mode()
        self._sync_speed()
        self._sync_highlight()
        if hasattr(self, "_playlist_task") and self.tabs.currentIndex() == 3:
            self._refresh_playlists(poll=True)
