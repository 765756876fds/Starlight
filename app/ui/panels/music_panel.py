"""音乐面板（本地 + 在线）。"""
import zlib
from typing import List

from PySide6.QtCore import QEvent, QThread, Qt, QTimer, QObject, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QStyledItemDelegate, QTabWidget, QVBoxLayout, QWidget,
)

from ..dialogs.identify_dialog import IdentifyDialog
from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.icon_utils import lucide_icon
from ..widgets.music_card_delegate import MusicCardDelegate, MusicCardRoles
from ...core.command_bus import CommandType, get_command_bus
from ...core.event_bus import get_event_bus
from ...music.humming import HummingService
from ...music.lyric_search import LyricIndex
from ...music.online.events import ONLINE_SEARCH_DONE, ONLINE_ERROR


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


class _RankListsWorker(QObject):
    """后台拉全插件榜单分组（bridge 串行遍历各插件，可能数秒，不能卡 UI）。"""
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, music):
        super().__init__()
        self._music = music

    def run(self):
        try:
            self.done.emit(self._music.get_top_lists() or {})
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
            self.done.emit(r or {"tracks": [], "is_end": True})
        except Exception as e:
            self.failed.emit(str(e))


class MusicPanel(QWidget):
    # P1 候选旁路：后台线程 publish → 此 Signal 桥到主线程渲染
    _search_event = Signal(dict)
    # 识曲语音命令（MUSIC_IDENTIFY）跨线程桥：voice 线程 emit → 主线程弹窗
    _identify_trigger = Signal()
    # 下载进度：后台线程 emit → 主线程渲染（跨线程安全）；pct None=无总量，-1=完成，-2=失败
    _dl_signal = Signal(str, object)  # key, pct

    def __init__(self, music=None, parent=None):
        super().__init__(parent)
        self._music = music
        self._tracks_by_row = []
        self._local_songs = []
        self._thread = None
        self._scan_thread = None
        self._voice_generation = None   # P1 候选旁路：当前 UI 展示的语音候选代际
        self._dl_markers = {}           # 下载进度行内标记：key -> {base, widget}
        self._dl_signal.connect(self._on_dl_progress)
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
        # 桌面歌词浮窗（首次点开关才创建；显式隐藏不销毁）
        self._lyrics_window = None
        # 哼唱识曲：语音命令跨线程桥 + 复用弹窗单例 + 服务（首次点按钮才建）
        self._identify_trigger.connect(self._on_identify_click)
        get_command_bus().register(CommandType.MUSIC_IDENTIFY, self._on_identify_cmd)
        self._identify_dialog = None
        self._identify_service = None

        root = QVBoxLayout(self)
        # 顶栏：右侧"歌词"开关（桌面歌词浮窗入口）
        top_bar = QHBoxLayout()
        top_bar.addStretch()
        self.btn_lyrics = QPushButton("歌词")
        self.btn_lyrics.setCheckable(True)
        self.btn_lyrics.setProperty("class", "secondary")
        self.btn_lyrics.setIcon(lucide_icon("book-open", 16, Tokens.TEXT_SECONDARY))
        self.btn_lyrics.clicked.connect(self._on_toggle_lyrics)
        top_bar.addWidget(self.btn_lyrics)
        self.btn_identify = QPushButton("识曲")
        self.btn_identify.setProperty("class", "secondary")
        self.btn_identify.setIcon(lucide_icon("mic", 16, Tokens.TEXT_SECONDARY))
        self.btn_identify.clicked.connect(self._on_identify_click)
        top_bar.addWidget(self.btn_identify)
        # 列表/网格 视图切换（Lucide grid/list，全局作用于全部歌曲列表）
        self.btn_view_toggle = QPushButton()
        self.btn_view_toggle.setCheckable(True)
        self.btn_view_toggle.setChecked(True)
        self.btn_view_toggle.setIcon(lucide_icon("grid", 16, Tokens.TEXT_PRIMARY))
        self.btn_view_toggle.setToolTip("切换 网格卡片 / 平铺列表（作用于全部歌曲列表）")
        self.btn_view_toggle.clicked.connect(self._toggle_view_mode)
        top_bar.addWidget(self.btn_view_toggle)
        root.addLayout(top_bar)
        tabs = self.tabs = QTabWidget()
        root.addWidget(tabs)
        tabs.currentChanged.connect(self._on_tab_changed)

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
        ll.addWidget(self.list_local, 1)
        # 本地列表"强制跳回"优化：正在播放的歌高亮（setCurrentRow）保留，
        # 但自动滚动改为——无操作 10 秒后单次跳回；用户任何操作（移动/滚轮/点击/悬停）
        # 重置计时（期间绝不跳回，防"想选别的一瞬间被拽走"）。
        self._local_anchor_row = -1
        self._local_jump_timer = QTimer(self)
        self._local_jump_timer.setSingleShot(True)
        self._local_jump_timer.setInterval(10000)
        self._local_jump_timer.timeout.connect(self._local_jump_to_current)
        self.list_local.setMouseTracking(True)
        self.list_local.installEventFilter(self)

        # 当前曲目（小标题级）
        self.lbl_track = QLabel("未播放")
        self.lbl_track.setAlignment(Qt.AlignCenter)
        self.lbl_track.setProperty("class", "title")
        ll.addWidget(self.lbl_track)

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
        ll.addWidget(self.slider_pos)
        ll.addWidget(self.lbl_time)

        # 播放控制：金字塔布局——高频操作（播放/暂停）放大居中，次要操作紧凑成行
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
        row_main.addStretch()
        row_main.addWidget(self.btn_prev)
        row_main.addWidget(self.btn_toggle)
        row_main.addWidget(self.btn_next)
        row_main.addStretch()
        ll.addLayout(row_main)

        # 次要操作（紧凑）+ 音量
        row_sub = QHBoxLayout()
        self.btn_highlight = QPushButton("高潮模式：关")
        self.btn_highlight.setObjectName("btn_highlight")
        self.btn_highlight.setCheckable(True)
        self.btn_highlight.setIcon(lucide_icon("music", 14, Tokens.TEXT_SECONDARY))
        self.btn_highlight.toggled.connect(self._on_highlight)
        self.btn_mode = QPushButton("顺序")
        self.btn_mode.setObjectName("btn_mode")
        self.btn_mode.setIcon(lucide_icon("repeat", 14, Tokens.TEXT_SECONDARY))
        self._modes = [
            ("sequential", "顺序"),
            ("shuffle", "随机"),
            ("repeat_all", "列表循环"),
            ("repeat_one", "单曲循环"),
        ]
        self._mode_idx = 0
        self.btn_clear_queue = QPushButton("清空队列")
        self.btn_clear_queue.setObjectName("btn_clear_queue")
        self.btn_clear_queue.setIcon(lucide_icon("trash", 14, Tokens.TEXT_SECONDARY))
        row_sub.addWidget(self.btn_highlight)
        row_sub.addWidget(self.btn_mode)
        row_sub.addWidget(self.btn_clear_queue)
        row_sub.addStretch()
        # 音量：MusicFree 悬浮极简风——平时仅喇叭图标，悬停/点击弹出垂直玻璃面板
        from ..widgets.glass_slider import GlassSlider, VolumeButton
        self.btn_volume = VolumeButton()
        self.slider_vol = GlassSlider(Qt.Vertical)
        self.slider_vol.setRange(0, 100)
        self.btn_volume.set_slider(self.slider_vol)
        row_sub.addWidget(self.btn_volume)
        ll.addLayout(row_sub)

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
        self._dl_results = self._setup_card_view(self.list_results)
        ol.addWidget(self.list_results)
        self.lbl_status = QLabel("输入关键词开始搜索")
        ol.addWidget(self.lbl_status)
        tabs.addTab(online, "在线")

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
        pl.addLayout(pl_left, 2)
        pl_right = QVBoxLayout()
        self.list_playlist_songs = QListWidget()
        self._dl_playlist_songs = self._setup_card_view(self.list_playlist_songs)
        pl_right.addWidget(self.list_playlist_songs, 1)
        self.lbl_playlist_status = QLabel("未选择歌单")
        self.lbl_playlist_status.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        pl_right.addWidget(self.lbl_playlist_status)
        pl.addLayout(pl_right, 3)
        tabs.addTab(playlist, "歌单")

        # ---- 榜单 Tab（左：平台 + 榜单列表；右：榜单歌曲）----
        rank = QWidget()
        rl = QHBoxLayout(rank)
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
        rl_right.addWidget(self.list_rank_songs, 1)
        self.lbl_rank_status = QLabel("加载榜单中…")
        self.lbl_rank_status.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        rl_right.addWidget(self.lbl_rank_status)
        rl.addLayout(rl_right, 3)
        tabs.addTab(rank, "榜单")

        # 播放按钮图标锁定（防 mpv 异步启动期间图标闪回）
        self._icon_locked = False
        self._poll_ticks = 0
        # 绑定
        if music is not None:
            self.btn_prev.clicked.connect(self._on_prev)
            self.btn_toggle.clicked.connect(self._on_toggle)
            self.btn_next.clicked.connect(self._on_next)
            self.btn_mode.clicked.connect(self._on_toggle_mode)
            self.btn_clear_queue.clicked.connect(self._on_clear_queue)
            self.slider_vol.valueChanged.connect(self._on_volume)
            self.slider_vol.sliderReleased.connect(self._on_volume_released)
            self.slider_pos.sliderReleased.connect(self._on_seek)
            self.btn_search.clicked.connect(self._do_search)
            self.edit_kw.returnPressed.connect(self._do_search)
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
            self._refresh_history()

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

    # ---- 卡片网格（只改 UI 绘制，不动数据流） ----
    def _setup_card_view(self, w: QListWidget) -> MusicCardDelegate:
        """歌曲类列表 → IconMode 卡片网格 + 自绘 Delegate。"""
        w.setViewMode(QListWidget.IconMode)
        w.setMovement(QListWidget.Static)
        w.setResizeMode(QListWidget.Adjust)
        w.setUniformItemSizes(True)
        w.setSpacing(Tokens.SPACING_MD)
        w.setMouseTracking(True)
        d = MusicCardDelegate(w)
        w.setItemDelegate(d)
        return d

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
                w.setItemDelegate(d if grid_on else QStyledItemDelegate(w))
                if grid_on:
                    w.setMovement(QListWidget.Static)
                    w.setResizeMode(QListWidget.Adjust)
                    w.setSpacing(Tokens.SPACING_MD)
            except RuntimeError:
                pass  # 视图已销毁
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

    def _focus_search(self):
        """Ctrl+F / Ctrl+K：聚焦当前 tab 的搜索框并全选。"""
        box = self.edit_local if self.tabs.currentIndex() == 0 else self.edit_kw
        box.setFocus()
        box.selectAll()

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

    def _show_lyric_results(self, songs: List[dict], kw: str):
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
        print(f"[UI] local play: {s.get('title')} id={s.get('id')}")
        try:
            self._music.play_song(s)
        except Exception as e:
            print(f"[UI] local play error: {e}")

    def _on_highlight(self, on: bool):
        self.btn_highlight.setText(f"高潮模式：{'开' if on else '关'}")
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
        # 点击瞬间锁定图标并立即翻转，mpv 异步启动期间旧状态不会再把它闪回
        cur = getattr(self, "_toggle_icon", None)
        if cur is None:                    # 首次点击：先对齐真实状态再翻转
            self._sync_toggle_icon()
            cur = getattr(self, "_toggle_icon", "play")
        self._icon_locked = True
        want = "pause" if cur == "play" else "play"
        self._toggle_icon = want
        self.btn_toggle.setIcon(lucide_icon(want, 18, "#ffffff"))
        try:
            self._music.toggle()
        except Exception:
            pass
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

    # ---- 本地列表"强制跳回"优化 ----
    def eventFilter(self, obj, event) -> bool:
        """用户操作本地列表（移动/滚轮/点击/进出悬停）→ 重置 10s 跳回计时，
        期间绝不自动滚动回当前播放曲。"""
        if obj is self.list_local and event.type() in (
                QEvent.MouseMove, QEvent.Wheel, QEvent.MouseButtonPress,
                QEvent.Enter, QEvent.Leave):
            self._local_jump_timer.start()
        return super().eventFilter(obj, event)

    def _local_jump_to_current(self) -> None:
        """无操作 10s 后：滚动回正在播放的歌曲（单次；此后用户操作会重新计时）。"""
        row = self._local_anchor_row
        if row < 0 or row >= self.list_local.count():
            return
        try:
            item = self.list_local.item(row)
            if item is not None:
                self.list_local.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        except Exception:
            pass

    # ---- 在线 ----
    def _do_search(self):
        kw = self.edit_kw.text().strip()
        if not kw or self._music is None:
            return
        # 防重入：旧搜索线程仍在运行 → 忽略本次（低频场景简单忽略即可，
        # 覆盖运行中线程引用同样会触发 QThread: Destroyed while running）。
        if self._thread is not None and self._thread.isRunning():
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
                sub = f"{t.artist} · {t.source_name}" if t.artist else (t.source_name or "")
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
                    "source_name": t.source_name,
                    "track_id": t.track_id,
                    "cover_url": getattr(t, "cover_url", None) or None,
                }, sub, f"net:{t.track_id}" if t.track_id else None))
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
                sub = f"{artist} · {src}" if artist and src else (artist or src)
                item.setData(Qt.UserRole, self._card_data({
                    "title": title,
                    "artist": artist,
                    "source_name": src,
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
        elif index == 4:
            self._load_top_lists()

    def _on_toggle_lyrics(self, checked: bool):
        """桌面歌词开关：开 → 创建浮窗并显示；关 → 隐藏（保留状态可再开）。"""
        if self._music is None:
            self.btn_lyrics.setChecked(False)
            return
        if self._lyrics_window is None:
            from app.ui.widgets.lyrics_window import LyricsWindow
            self._lyrics_window = LyricsWindow(self._music)
            # 设置页"🎤 桌面歌词"改动 → 窗口即时生效（幂等注册；窗口显式隐藏不销毁，注册一次即可）
            from app.ui.panels.settings_panel import register_lyrics_prefs_listener
            register_lyrics_prefs_listener(self._lyrics_window.apply_prefs)
        if checked:
            self._lyrics_window.start()
            self._lyrics_window.show()
        else:
            self._lyrics_window.stop()
            self._lyrics_window.hide()

    # ------------------------------------------------------------------ 哼唱识曲
    def _on_identify_cmd(self, command):
        """语音命令 MUSIC_IDENTIFY 处理器（voice 线程）→ 跨线程桥到主线程弹窗。"""
        self._identify_trigger.emit()

    def _get_identify_service(self) -> HummingService:
        """惰性创建识曲服务（scan_dirs / 指纹库路径 / 扩展名来自 music 配置）。"""
        if self._identify_service is None:
            music = self._music
            scan_dirs = list(getattr(music, "scan_dirs", None) or [])
            config = getattr(music, "config", None) or {}
            db_path = config.get("fingerprints", "data/music/fingerprints.db")
            ext = list(config.get("extensions") or []) or None
            self._identify_service = HummingService(
                scan_dirs=scan_dirs, sqlite_path=db_path, extensions=ext)
        return self._identify_service

    def _on_identify_click(self):
        """按钮或语音触发识曲：显示弹窗并开始（弹窗内部防重入）。"""
        if self._identify_dialog is None:
            self._identify_dialog = IdentifyDialog(
                self._get_identify_service(), on_play=self._on_identify_play, parent=self)
        self._identify_dialog.show()
        self._identify_dialog.raise_()
        self._identify_dialog.start()

    def _on_identify_play(self, result):
        """识别成功 → 按指纹 filepath 回本地库 → play_song（失败静默提示）。"""
        music = self._music
        if music is None:
            return
        filepath = (result or {}).get("filepath") or ""
        if not filepath:
            return
        try:
            song = music.db.get_song_by_path(filepath)
            if not song:
                print(f"[Identify] 指纹命中但本地库无该歌: {filepath}")
                return
            music.play_song(song)
        except Exception as e:
            print(f"[Identify] 播放失败: {e}")

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
        row = item.data(Qt.UserRole)
        if not row or self._music is None:
            return
        ok = False
        try:
            ok = self._music.play_history_item(row)
        except Exception as e:
            print(f"[MusicPanel] history play error: {e}")
        if ok:
            self.lbl_status.setText(f"正在播放：{row.get('title', '')}")
        else:
            self.lbl_status.setText("该歌已不可用")

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

    def _refresh_playlists(self):
        """重载左侧歌单列表（保持当前选中）。"""
        if self._music is None:
            return
        try:
            pls = self._music.get_playlists()
        except Exception as e:
            print(f"[MusicPanel] get_playlists error: {e}")
            pls = []
        current_id = None
        it = self.list_playlists.currentItem()
        if it is not None:
            d = it.data(Qt.UserRole)
            if d:
                current_id = d.get("id")
        self.list_playlists.clear()
        for p in pls:
            item = QListWidgetItem(p.get("name", "?"))
            item.setData(Qt.UserRole, p)
            self.list_playlists.addItem(item)
            if p.get("id") == current_id:
                self.list_playlists.setCurrentItem(item)
        if pls and self.list_playlists.currentItem() is None:
            self.list_playlists.setCurrentRow(0)
        self._refresh_playlist_songs()

    def _refresh_playlist_songs(self):
        """渲染选中歌单的歌（按加入时间）。"""
        self.list_playlist_songs.clear()
        it = self.list_playlists.currentItem()
        if it is None:
            self.lbl_playlist_status.setText("未选择歌单")
            return
        p = it.data(Qt.UserRole)
        if not p:
            self.lbl_playlist_status.setText("未选择歌单")
            return
        try:
            rows = self._music.get_playlist_songs(p.get("id"))
        except Exception as e:
            print(f"[MusicPanel] get_playlist_songs error: {e}")
            rows = []
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
                print(f"[MusicPanel] playlist item#{i}: "
                      f"title={title!r} artist={artist!r} source_type={st!r} "
                      f"music_id={row.get('music_id')!r} "
                      f"cover_url={row.get('cover_url')!r} path={row.get('path')!r}")
            src = "本地" if st == "local" else (sn or "在线")
            item = QListWidgetItem(f"{title} - {artist} [{src}]")
            mid = row.get("music_id") or row.get("id")
            item.setData(Qt.UserRole, self._card_data(
                row,
                f"{artist} · {src}" if artist else src,
                f"local:{mid}" if mid else None))
            self.list_playlist_songs.addItem(item)
        self.lbl_playlist_status.setText(f"{len(rows)} 首")

    def _on_playlist_selected(self, current, previous):
        """歌单列表选中变化 → 刷新右侧歌曲。"""
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
        row = item.data(Qt.UserRole)
        if not row or self._music is None:
            return
        ok = False
        try:
            ok = self._music.play_playlist_item(row)
        except Exception as e:
            print(f"[MusicPanel] playlist song play error: {e}")
        if ok:
            self.lbl_status.setText(f"正在播放：{row.get('title', '')}")
        else:
            self.lbl_status.setText("该歌已不可用")

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
            self._rank_thread.start()
        except Exception as e:
            self.lbl_rank_status.setText(f"榜单加载失败: {e}")

    def _on_rank_lists_done(self, groups: dict):
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
        # 最新请求补偿：运行期间又有切 tab → 自动补拉一轮
        if self._pending_rank_lists:
            self._pending_rank_lists = False
            self._load_top_lists()

    def _on_rank_lists_failed(self, err: str):
        self.lbl_rank_status.setText(f"榜单加载失败: {err}")
        if self._pending_rank_lists:
            self._pending_rank_lists = False
            self._load_top_lists()

    def _on_rank_source_selected(self, current, previous):
        """平台选中变化 → 下方榜单列表（该平台全部分组的榜单）。"""
        if current is None:
            return
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
        # 防重入：上一轮详情线程仍在运行 → 记最新请求（覆盖旧的），
        # 旧线程 done/failed 后自动补加载。绝不覆盖运行中线程引用（崩溃修复）。
        if self._rank_detail_thread is not None and self._rank_detail_thread.isRunning():
            self._pending_rank_detail = {"source": source, "list_id": list_id, "title": title}
            self.lbl_rank_status.setText(f"加载「{title}」…")
            return
        self._pending_rank_detail = None
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
            self._rank_detail_thread.start()
        except Exception as e:
            self.lbl_rank_status.setText(f"榜单详情加载失败: {e}")

    def _on_rank_detail_done(self, payload: dict):
        tracks = (payload or {}).get("tracks") or []
        self._rank_tracks = tracks
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
            }, t.artist, f"net:{t.track_id}" if t.track_id else None))
            self.list_rank_songs.addItem(item)
        n = len(tracks)
        self.lbl_rank_status.setText(f"共 {n} 首，双击播放" if n else "榜单无歌曲")
        # 最新请求补偿：加载期间用户又点了其他榜单 → 自动加载最新的
        if self._pending_rank_detail:
            req = self._pending_rank_detail
            self._pending_rank_detail = None
            self._load_rank_detail(req["source"], req["list_id"], req["title"])

    def _on_rank_detail_failed(self, err: str):
        self.lbl_rank_status.setText(f"榜单详情加载失败: {err}")
        if self._pending_rank_detail:
            req = self._pending_rank_detail
            self._pending_rank_detail = None
            self._load_rank_detail(req["source"], req["list_id"], req["title"])

    def _on_rank_song_play(self, item: QListWidgetItem):
        track = item.data(Qt.UserRole)
        if not track or self._music is None:
            return
        try:
            ok = self._music.play_online(track)
        except Exception as e:
            ok = False
            self.lbl_rank_status.setText(f"播放失败: {e}")
            return
        if ok:
            name = track.get("title") if isinstance(track, dict) else getattr(track, "title", "")
            self.lbl_rank_status.setText(f"正在播放：{name}")
        else:
            self.lbl_rank_status.setText("该曲目无法播放")

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
            act = sub.addAction(p.get("name", "?"))
            act.triggered.connect(
                lambda _=False, pid=p.get("id"): self._add_to_playlist(pid, song))
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
        act_next = menu.addAction("下一首播放")
        act_next.triggered.connect(lambda: self._on_next_play(song))
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
                    self._music.is_downloaded(song.get("title", ""), song.get("artist", "")))
            except Exception:
                downloaded = False
            if downloaded:
                act_del_dl = menu.addAction("删除本地下载")
                act_del_dl.triggered.connect(lambda: self._delete_download(song))
            else:
                act_dl = menu.addAction("下载到本地")
                act_dl.triggered.connect(lambda: self._on_download_song(song))
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

    def _on_next_play(self, song: dict):
        if self._music is None:
            return
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
        try:
            self._music.add_next(d)
        except Exception as e:
            print(f"[MusicPanel] add_next error: {e}")
            return
        self.lbl_status.setText(f"已插到下一首：{song.get('title', '')}")

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
        t = self._tracks_by_row[idx]
        song = {
            "title": t.title,
            "artist": t.artist,
            "source_type": "online",
            "source_name": t.source_name,
            "track_id": t.track_id,
            "music_id": 0,
            "id": 0,
            "path": "",
        }
        menu = self._build_song_menu(song)
        menu.exec_(self.list_results.mapToGlobal(pos))

    def _on_download_song(self, song: dict):
        """在线歌下载入队：行内标记 + 状态栏；进度经 _dl_signal 回主线程。"""
        if self._music is None:
            return
        key = f"{song.get('title', '')}|{song.get('artist', '')}".strip('|')
        base = None
        widget = None
        # 定位卡片标记目标：在线 tab（按 _tracks_by_row 匹配）→ 歌单列表（按 UserRole 匹配）
        try:
            for i, t in enumerate(self._tracks_by_row):
                if t.title == song.get("title") and t.artist == song.get("artist"):
                    item = self.list_results.item(i)
                    if item is not None:
                        base = t.title
                        widget = "online"
                    break
        except Exception:
            pass
        if widget is None:
            for i in range(self.list_playlist_songs.count()):
                item = self.list_playlist_songs.item(i)
                row = item.data(Qt.UserRole) if item is not None else None
                if row is not None and row.get("title") == song.get("title"):
                    base = song.get("title")
                    widget = "playlist"
                    break
        self._dl_markers[key] = {"base": base, "widget": widget}

        def on_progress(k, pct):
            self._dl_signal.emit(k, pct)

        def on_done(k, path):
            self._dl_signal.emit(k, -1)
            self.lbl_status.setText(f"已下载：{song.get('title', '')}")
            self._refresh_local_list(self.edit_local.text().strip())
            if self.tabs.currentIndex() == 3:
                self._refresh_playlists()

        def on_fail(k, reason):
            self._dl_signal.emit(k, -2)
            self.lbl_status.setText(f"下载失败：{reason}")

        def on_exists(k):
            self.lbl_status.setText("该歌已下载，跳过（不覆盖）")

        self.lbl_status.setText(f"开始下载：{song.get('title', '')}")
        self._music.download_song(song, {
            "on_progress": on_progress,
            "on_done": on_done,
            "on_fail": on_fail,
            "on_exists": on_exists,
        })

    def _on_dl_progress(self, key, pct):
        """主线程：更新卡片状态徽标（跨线程 Signal 桥；不拼接列表文本）。"""
        if pct in (-1, -2):
            self._restore_marker(key)
            return
        label = "下载中…" if pct is None else f"下载中 {pct}%"
        m = self._dl_markers.get(key)
        if not m or not m.get("base"):
            return
        try:
            if m.get("widget") == "online":
                for i, t in enumerate(self._tracks_by_row):
                    if t.title == m["base"]:
                        item = self.list_results.item(i)
                        if item is not None:
                            item.setData(MusicCardRoles.StatusRole, label)
                            self.list_results.viewport().update()
                        return
            elif m.get("widget") == "playlist":
                for i in range(self.list_playlist_songs.count()):
                    item = self.list_playlist_songs.item(i)
                    row = item.data(Qt.UserRole)
                    if row is not None and row.get("title") == m["base"]:
                        item.setData(MusicCardRoles.StatusRole, label)
                        self.list_playlist_songs.viewport().update()
                        return
        except Exception:
            pass

    def _restore_marker(self, key):
        """完成/失败：清除卡片状态徽标，移除标记。"""
        m = self._dl_markers.get(key)
        if not m or not m.get("base"):
            self._dl_markers.pop(key, None)
            return
        try:
            if m.get("widget") == "online":
                for i, t in enumerate(self._tracks_by_row):
                    if t.title == m["base"]:
                        item = self.list_results.item(i)
                        if item is not None:
                            item.setData(MusicCardRoles.StatusRole, None)
                            self.list_results.viewport().update()
                        break
            elif m.get("widget") == "playlist":
                for i in range(self.list_playlist_songs.count()):
                    item = self.list_playlist_songs.item(i)
                    row = item.data(Qt.UserRole)
                    if row is not None and row.get("title") == m["base"]:
                        item.setData(MusicCardRoles.StatusRole, None)
                        self.list_playlist_songs.viewport().update()
                        break
        except Exception:
            pass
        self._dl_markers.pop(key, None)

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
                # 自动定位到本地列表中正在播放的歌曲：只高亮（setCurrentRow），
                # 滚动交给 _local_jump_timer（无操作 10s 后单次跳回；用户操作期间绝不滚）
                if not song.get("online"):
                    song_id = song.get("id")
                    if song_id:
                        for i in range(self.list_local.count()):
                            item = self.list_local.item(i)
                            s = item.data(Qt.UserRole)
                            if s and s.get("id") == song_id:
                                self.list_local.setCurrentRow(i)
                                if self._local_anchor_row != i:
                                    self._local_anchor_row = i
                                    self._local_jump_timer.start()  # 新歌/新行 → 重新计时
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
            label = "高潮模式：开" if hl else "高潮模式：关"
            if self.btn_highlight.isChecked() != hl:
                self.btn_highlight.setChecked(hl)
            if self.btn_highlight.text() != label:
                self.btn_highlight.setText(label)
                print(f"[UI] 高潮模式按钮同步: {label}")
