"""设置 tab：音乐目录管理 / 语音 TTS / LoL 开关 / 界面刷新间隔。

- 音乐目录：config/music_dirs.json（路径/启用/格式过滤/排除子目录），[扫描全部] 走
  music.scanner 直接赋值 + QThread 调 music.rescan()（不碰 music 业务逻辑）
- TTS：更新 settings.yaml tts 节 + 调 TTSController set_voice/set_rate/set_volume 立即生效
- LoL：连招总开关（不持久化）、仅 LoL 前台 + 装备提醒（持久化 config/ui_prefs.json）
- 界面：刷新间隔（持久化 ui_prefs.json，经 on_refresh_changed 回调应用到各面板 timer）
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFontComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme.colors import Colors
from app.ui.theme.tokens import Tokens
from app.ui.widgets.glass_slider import GlassSlider

from ...lol.data.lol_data_updater import update_all as _update_lol_data
from ..settings_store import (
    ALL_EXTENSIONS,
    load_lyrics_prefs,
    load_music_dirs,
    load_ui_prefs,
    reset_music_dirs,
    reset_ui_prefs,
    save_lyrics_prefs,
    save_music_dirs,
    save_settings_yaml,
    save_ui_prefs,
)
from ..settings_store import (
    SETTINGS_PATH as _SETTINGS_PATH,
)
from ..widgets.glass_dialog import GlassDialog
from ..widgets.glass_msg import glass_question, glass_warning
from ..widgets.icon_utils import lucide_icon
from ..widgets.toggle_button import ToggleButton


class NoWheelSlider(GlassSlider):
    """滚轮悬停不改值：wheelEvent 忽略 → 事件冒泡给父 QScrollArea 滚动。
    Qt 滚轮事件发给悬停控件（与键盘焦点无关），QSlider 默认消费 wheel 改值，
    设置页滚动时路过滑块会被误触。基类 GlassSlider = 纯 QPainter 自绘（无白底）。"""

    def wheelEvent(self, event):
        event.ignore()

# ---------- 桌面歌词偏好监听器（设置页保存 → 已开的歌词窗即时生效） ----------
_LYRICS_LISTENERS: list = []


def register_lyrics_prefs_listener(fn: Callable) -> None:
    """注册歌词窗 apply_prefs 回调（music_panel 创建窗口时注册；幂等）。"""
    if fn not in _LYRICS_LISTENERS:
        _LYRICS_LISTENERS.append(fn)


def _notify_lyrics_prefs_changed(update: dict | None = None) -> None:
    """保存后通知全部监听器（单个失败不影响其他；窗口未开则无监听，下次构造读新值）。

    **位置门控（防"点锁定/改字体瞬间跑回中央"）**：本次更新不含 pos 键
    （设置页普通改动：锁定/字体/字号/颜色/透明度/模式）→ 通知给监听器的 prefs
    剥离 pos_x/pos_y——窗口 apply_prefs 的"仅显式含 pos 才移动"门控不触发，
    绝不挪动/绝不 recenter。只有拖拽保存/重置位置（update 含 pos 键）才保留 pos。
    """
    try:
        prefs = load_lyrics_prefs()
    except Exception:
        return
    if update is not None and "pos_x" not in update and "pos_y" not in update:
        prefs = {k: v for k, v in prefs.items() if k not in ("pos_x", "pos_y")}
    for fn in _LYRICS_LISTENERS:
        try:
            fn(prefs)
        except Exception:
            pass

# ---------- TTS 音色常量表（按引擎） ----------

# Qwen-Audio-TTS（qwen-audio-3.1-tts-flash）官方音色 28 个：(voice 参数, 中文描述)
# 显示格式"代号（描述）"，无描述的只显示代号
_QWEN_VOICES = [
    ("longanlingxin_v3.1", "龙安灵心·女"),   # 默认
    ("longanhuan_v3.1", "龙安欢·女"),
    ("longanfengyue_v3.1", "龙安风悦·女"),
    ("xunanchuan_v3.1", "许南川·男"),
    ("yuxiaoyun_v3.1", "于小云·女"),
    ("anmingyuan_v3.1", "安明远·男"),
    ("wenhuaiqing_v3.1", "温怀清·女"),
    ("anruorou_v3.1", "安若柔·女"),
    ("guyunshu_v3.1", "顾云舒·女"),
    ("huozhuoshi_v3.1", "霍拙石·男"),
    ("yeqinghe_v3.1", "叶清禾·女"),
    ("yunhuanhuan_v3.1", "云欢欢·女"),
    ("xuxiaoqiao_v3.1", "徐小俏·女"),
    ("baianran_v3.1", "白安然·女"),
    ("xuyanchu_v3.1", "许言初·女"),
    ("yezhiqing_v3.1", "叶知晴·女"),
    ("Emily_v3.1", "Emily·英女"),
    ("Luna_v3.1", "Luna·英女"),
    ("Eric_v3.1", "Eric·英男"),
    ("Luca_v3.1", "Luca·英男"),
    ("Abby_v3.1", "Abby·美女"),
    ("Beth_v3.1", "Beth·美女"),
    ("Betty_v3.1", "Betty·美女"),
    ("Cindy_v3.1", "Cindy·美女"),
    ("Donna_v3.1", "Donna·美女"),
    ("Andy_v3.1", "Andy·美男"),
    ("Brian_v3.1", "Brian·美男"),
    ("David_v3.1", "David·美男"),
]

# Edge TTS 常用音色（现有 11 个保留；Edge 403 时动态拉不到，写死）
_EDGE_VOICES = [
    "zh-CN-XiaoxiaoNeural",   # 晓晓（女）
    "zh-CN-XiaoyiNeural",     # 晓伊（女）
    "zh-CN-YunxiNeural",      # 云希（男）
    "zh-CN-YunjianNeural",    # 云健（男）
    "zh-CN-YunyangNeural",    # 云扬（男）
    "zh-CN-XiaochenNeural",   # 晓辰（女）
    "zh-CN-XiaohanNeural",    # 晓涵（女）
    "zh-CN-XiaomengNeural",   # 晓梦（女）
    "zh-TW-HsiaoChenNeural",  # 曉臻（女，台湾）
    "en-US-AriaNeural",       # Aria（英女）
    "en-US-GuyNeural",        # Guy（英男）
]

_QWEN_DEFAULT_VOICE = "longanlingxin_v3.1"
_EDGE_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def _voice_label(vid: str, desc: str) -> str:
    """音色下拉显示："代号（描述）"；无描述只显示代号。"""
    return f"{vid}（{desc}）" if desc else vid


def _qwen_rate(n: int) -> float:
    """Qwen 语速映射：滑块 ±100 → rate 0.5~2.0。"""
    return 1.0 + n / 100.0


def _qwen_vol(n: int) -> int:
    """Qwen 音量映射：滑块 0~100 → volume 0~100。"""
    return int(n)


def _edge_pct(n: int) -> str:
    """Edge 百分比映射：滑块 → "+N%"。"""
    return f"{n:+d}%"


def _probe_url(url: str, headers, timeout: float = 5.0) -> str:
    """探测 URL 能否播放：GET + Range: bytes=0-1024（只拉前 1KB，不下整首）。
    200/206 → ✓；403/404 → ✗(403)；其他 → ✗(原因)。
    带插件 headers（Referer）——否则开心汽水裸请求必 403，而 mpv 带 Referer
    可能能播，探测结果要反映"真实播放能力"。
    """
    h = {"Range": "bytes=0-1024", "User-Agent": "Mozilla/5.0"}
    if headers:
        for k in ("Referer", "User-Agent", "Cookie", "Authorization"):
            if k in headers:
                h[k] = headers[k]
    req = urllib.request.Request(url, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            out = "✓" if code in (200, 206) else f"✗({code})"
            print(f"[probe] {url[:60]} -> {out} ({code})", flush=True)
            return out
    except urllib.error.HTTPError as e:
        print(f"[probe] {url[:60]} -> HTTPError {e.code}", flush=True)
        return f"✗({e.code})"
    except Exception as e:
        print(f"[probe] {url[:60]} -> {type(e).__name__}", flush=True)
        return f"✗({type(e).__name__})"


class _ScanWorker(QObject):
    """后台扫描 worker（复用 QThread 模式，避免大目录扫描卡 UI）。"""
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


class _DirEditDialog(GlassDialog):
    """单个目录编辑：格式过滤多选 + 排除子目录。"""

    def __init__(self, entry: dict, parent=None):
        super().__init__("编辑音乐目录", parent)
        self._exts = set(entry.get("extensions") or ALL_EXTENSIONS)
        root = self.body_layout
        root.addWidget(QLabel(f"<b>{entry.get('path', '')}</b>"))
        root.addWidget(QLabel("格式过滤（勾选要扫描的格式）："))
        self._boxes = []
        frow = QHBoxLayout()
        for ext in ALL_EXTENSIONS:
            cb = QCheckBox(ext)
            cb.setChecked(ext in self._exts)
            self._boxes.append(cb)
            frow.addWidget(cb)
        root.addLayout(frow)
        self.cb_exclude = QCheckBox("排除子目录（只扫该目录本身，不递归）")
        self.cb_exclude.setChecked(bool(entry.get("exclude_subdirs", False)))
        root.addWidget(self.cb_exclude)
        btns = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_ok.setProperty("class", "primary")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        root.addLayout(btns)

    def result_entry(self) -> dict:
        exts = [b.text() for b in self._boxes if b.isChecked()]
        return {"extensions": exts, "exclude_subdirs": self.cb_exclude.isChecked()}


class SettingsPanel(QWidget):
    # 后台线程 → 主线程回传（worker 线程没有 Qt 事件循环，不能用 QTimer.singleShot 从
    # 后台线程调度 UI 更新——跨线程必须走 Signal，AutoConnection 自动转 QueuedConnection）
    _test_done = Signal(str, str)       # (插件名, 结果文本)
    _refresh_done = Signal(bool, int)   # (ok, 插件数)
    _data_done = Signal(str)            # 数据更新完成消息（跨线程回主线程）
    _updates_done = Signal(str, object)  # (状态文本, 检查结果列表)

    def __init__(self, music=None, tts=None, combo_manager=None,
                 equip_toggle: Callable[[bool], None] | None = None,
                 on_refresh_changed: Callable[[int], None] | None = None,
                 overlay_toggle: Callable[[bool], None] | None = None,
                 overlay_enabled: bool = True,
                 parent=None):
        super().__init__(parent)
        self._music = music
        self._tts = tts
        self._combo = combo_manager
        self._equip_toggle = equip_toggle
        self._on_refresh_changed = on_refresh_changed
        self._overlay_toggle = overlay_toggle
        self._overlay_enabled = bool(overlay_enabled)
        self._scan_thread = None
        self._js_source = None
        self._reloading = False
        self._checking_updates = False
        self._plugin_rows: dict = {}
        self._test_btns: dict = {}
        self._update_btns: dict = {}
        self._plugin_updates: dict = {}   # name -> check_updates 结果（重渲染时合并进行）
        self._test_done.connect(self._on_test_done)
        self._refresh_done.connect(self._on_refresh_done)
        self._updates_done.connect(self._on_updates_done)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        tab_music = self._make_tab_page("音乐")
        tab_voice = self._make_tab_page("语音")
        tab_game = self._make_tab_page("游戏")
        tab_ui = self._make_tab_page("界面")

        # Tab 1 音乐：音乐目录管理 + 内置插件
        self._build_music_group(tab_music)
        self._build_plugins_group(tab_music)
        self._build_movie_group(tab_music)
        self._build_lyrics_group(tab_music)
        tab_music.addStretch(1)
        # Tab 2 语音：语音 TTS
        self._build_tts_group(tab_voice)
        tab_voice.addStretch(1)
        # Tab 3 游戏：LoL
        self._build_lol_group(tab_game)
        tab_game.addStretch(1)
        # Tab 4 界面：界面 + 恢复默认（底部）
        self._build_ui_group(tab_ui)
        tab_ui.addStretch(1)
        self._build_reset_row(tab_ui)

    def _make_tab_page(self, title: str) -> QVBoxLayout:
        """新建一个 tab 页：QScrollArea → QWidget → QVBoxLayout。
        返回 layout 供 _build_* 填充；每页独立滚动。"""
        page = QScrollArea()
        page.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setSpacing(Tokens.SPACING_LG)  # 组间 24px（8px 网格：组间用 LG）
        lay.setContentsMargins(Tokens.SPACING_SM, Tokens.SPACING_SM,
                               Tokens.SPACING_SM, Tokens.SPACING_SM)
        page.setWidget(inner)
        self._tabs.addTab(page, title)
        return lay

    @staticmethod
    def _label(text: str) -> QLabel:
        """统一表单 Label：120px 固定宽 + 右对齐（组内 Label/Input 水平对齐规范）。"""
        l = QLabel(text)
        l.setFixedWidth(120)
        l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return l

    # ---------- 🎵 音乐目录管理 ----------

    def _build_music_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("音乐目录管理")
        lay = QVBoxLayout(box)
        lay.setSpacing(Tokens.SPACING_SM)  # 组内 8px 网格

        # 目录列表容器：QScrollArea + 行 widget（避开 QListWidget setItemWidget 压缩）
        list_scroll = QScrollArea()
        list_scroll.setWidgetResizable(True)
        list_scroll.setMinimumHeight(100)  # 有内容时按内容撑高，无内容不占大块
        self._dirs_container = QWidget()
        self._dirs_layout = QVBoxLayout(self._dirs_container)
        self._dirs_layout.setContentsMargins(Tokens.SPACING_XS, Tokens.SPACING_XS,
                                             Tokens.SPACING_XS, Tokens.SPACING_XS)
        self._dirs_layout.setSpacing(Tokens.SPACING_XS)
        list_scroll.setWidget(self._dirs_container)
        lay.addWidget(list_scroll)

        btns = QHBoxLayout()
        self.btn_add_dir = QPushButton("+ 添加目录")
        self.btn_add_dir.clicked.connect(self._on_add_dir)
        self.btn_scan_all = QPushButton("扫描全部")
        self.btn_scan_all.setProperty("class", "primary")
        self.btn_scan_all.clicked.connect(self._on_scan_all)
        self.btn_source_manage = QPushButton("音源管理…")
        self.btn_source_manage.clicked.connect(self._on_source_manage)
        self.lbl_scan_status = QLabel("")
        self.lbl_scan_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        btns.addWidget(self.btn_add_dir)
        btns.addWidget(self.btn_scan_all)
        btns.addWidget(self.btn_source_manage)
        btns.addWidget(self.lbl_scan_status)
        btns.addStretch()
        lay.addLayout(btns)
        root.addWidget(box)

        # 下载目录（在线歌下载到本地；默认 = 第一个音乐目录）
        dl_box = QGroupBox("下载目录（在线歌下载）")
        dl_lay = QVBoxLayout(dl_box)
        dl_row = QHBoxLayout()
        self.edit_download_dir = QLineEdit()
        self.edit_download_dir.setPlaceholderText("默认 = 第一个音乐目录")
        self.edit_download_dir.setText(self._load_download_dir())
        btn_dl_browse = QPushButton("浏览…")
        btn_dl_browse.clicked.connect(self._on_browse_download_dir)
        btn_dl_save = QPushButton("保存")
        btn_dl_save.clicked.connect(self._on_save_download_dir)
        dl_row.addWidget(self.edit_download_dir, 1)
        dl_row.addWidget(btn_dl_browse)
        dl_row.addWidget(btn_dl_save)
        dl_lay.addLayout(dl_row)
        self.lbl_dl_status = QLabel("")
        self.lbl_dl_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        dl_lay.addWidget(self.lbl_dl_status)
        root.addWidget(dl_box)
        self._dirs: list = load_music_dirs()
        self._render_dirs()

    def _load_download_dir(self) -> str:
        """读 settings.yaml music.download_dir（无 → 空，占位符提示默认）。"""
        try:
            import yaml
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("music", {}).get("download_dir", "") or ""
        except Exception:
            return ""

    def _on_browse_download_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择下载目录")
        if path:
            self.edit_download_dir.setText(path)

    def _on_save_download_dir(self):
        path = self.edit_download_dir.text().strip()
        if not path:
            self.lbl_dl_status.setText("请输入下载目录")
            return
        ok = save_settings_yaml({"music": {"download_dir": path}})
        self.lbl_dl_status.setText("已保存（下次启动生效）" if ok else "保存失败")

    # ---------- 🎬 电影（Assrt 字幕） ----------

    def _build_movie_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("电影（字幕）")
        lay = QVBoxLayout(box)
        lay.setSpacing(Tokens.SPACING_SM)  # 组内 8px 网格

        row1 = QHBoxLayout()
        self.edit_subtitle_token = QLineEdit()
        self.edit_subtitle_token.setPlaceholderText("Assrt Token（api.assrt.net 注册获取）")
        self.edit_subtitle_token.setText(self._load_movie_token())
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._on_save_movie_settings)
        row1.addWidget(self._label("字幕 Token"))
        row1.addWidget(self.edit_subtitle_token, 1)
        row1.addWidget(btn_save)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.cb_auto_subtitle = QCheckBox("播放时自动搜一次字幕（每片一次）")
        self.cb_auto_subtitle.setChecked(self._load_movie_auto_subtitle())
        self.cb_auto_subtitle.toggled.connect(self._on_save_movie_settings)
        row2.addWidget(self.cb_auto_subtitle)
        row2.addStretch()
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        btn_sources = QPushButton("电影源管理（多源 / 测源 / 增删 / 设当前）")
        btn_sources.clicked.connect(self._on_movie_sources)
        row3.addWidget(btn_sources)
        row3.addStretch()
        lay.addLayout(row3)

        self.lbl_movie_status = QLabel("")
        self.lbl_movie_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        lay.addWidget(self.lbl_movie_status)
        root.addWidget(box)

    def _on_movie_sources(self):
        """打开电影源管理对话框（多源/测源/增删/设当前）。"""
        try:
            from ..dialogs.movie_source_dialog import MovieSourceDialog
            dlg = MovieSourceDialog(self)
            dlg.exec()
        except Exception as e:
            from app.ui.theme.colors import Colors  # noqa: F401
            print(f"[Settings] 打开电影源管理失败: {e}")
            try:
                glass_warning(self, "提示", f"打开电影源管理失败: {e}")
            except Exception:
                pass

    def _load_movie_token(self) -> str:
        """读 settings.yaml movie.assrt_token。"""
        try:
            import yaml
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("movie", {}).get("assrt_token", "") or ""
        except Exception:
            return ""

    def _load_movie_auto_subtitle(self) -> bool:
        """读 settings.yaml movie.auto_subtitle（默认关）。"""
        try:
            import yaml
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return bool(cfg.get("movie", {}).get("auto_subtitle", False))
        except Exception:
            return False

    def _on_save_movie_settings(self):
        ok = save_settings_yaml({"movie": {
            "assrt_token": self.edit_subtitle_token.text().strip(),
            "auto_subtitle": self.cb_auto_subtitle.isChecked(),
        }})
        self.lbl_movie_status.setText("已保存（下次启动生效）" if ok else "保存失败")

    # ---------- 🎤 桌面歌词（批 3b 自定义面板） ----------

    def _build_lyrics_group(self, root: QVBoxLayout) -> None:
        """字体/字号/颜色/透明度/单双行/锁定/重置位置；改动即时生效（保存 + 通知歌词窗）。"""
        # 构造期 guard：setCurrentFont/setValue/toggled 会触发 *Changed 信号，
        # 此时部分控件未就绪且不应保存——末尾才置 True，构造期信号全部忽略
        self._lyrics_group_ready = False
        prefs = load_lyrics_prefs()
        box = QGroupBox("桌面歌词")
        lay = QVBoxLayout(box)
        lay.setSpacing(Tokens.SPACING_SM)  # 组内 8px 网格

        row_font = QHBoxLayout()
        self.cmb_lyrics_font = QFontComboBox()
        self.cmb_lyrics_font.setCurrentFont(QFont(str(prefs.get("font") or "Microsoft YaHei")))
        self.cmb_lyrics_font.setMaximumWidth(240)
        self.cmb_lyrics_font.currentFontChanged.connect(self._on_lyrics_changed)
        row_font.addWidget(self._label("字体"))
        row_font.addWidget(self.cmb_lyrics_font, 1)
        lay.addLayout(row_font)

        row_size = QHBoxLayout()
        self.spin_lyrics_size = QSpinBox()
        self.spin_lyrics_size.setRange(12, 48)
        self.spin_lyrics_size.setValue(int(prefs.get("font_size") or 24))
        self.spin_lyrics_size.valueChanged.connect(self._on_lyrics_changed)
        row_size.addWidget(self._label("字号"))
        row_size.addWidget(self.spin_lyrics_size)
        row_size.addStretch()
        lay.addLayout(row_size)

        row_color = QHBoxLayout()
        self.btn_lyrics_color = QPushButton()
        self.btn_lyrics_color.setFixedSize(28, 22)
        self.btn_lyrics_color.setToolTip("点击选择歌词颜色（当前行）")
        self.btn_lyrics_color.clicked.connect(self._on_lyrics_color)
        self._lyrics_color = QColor(str(prefs.get("color") or "#FAB446"))
        self._refresh_lyrics_color_btn()
        row_color.addWidget(self._label("颜色"))
        row_color.addWidget(self.btn_lyrics_color)
        row_color.addStretch()
        lay.addLayout(row_color)

        row_op = QHBoxLayout()
        self.slider_lyrics_opacity = NoWheelSlider(Qt.Horizontal)
        self.slider_lyrics_opacity.setRange(0, 100)
        self.slider_lyrics_opacity.setValue(int(prefs.get("bg_opacity") or 0))
        self.slider_lyrics_opacity.valueChanged.connect(self._on_lyrics_opacity)
        self.lbl_lyrics_opacity = QLabel(f"{self.slider_lyrics_opacity.value()}%")
        row_op.addWidget(self._label("背景透明度"))
        row_op.addWidget(self.slider_lyrics_opacity, 1)
        row_op.addWidget(self.lbl_lyrics_opacity)
        lay.addLayout(row_op)

        row_mode = QHBoxLayout()
        self.cmb_lyrics_mode = QComboBox()
        self.cmb_lyrics_mode.addItem("3 行上下文", "triple")
        self.cmb_lyrics_mode.addItem("单行", "single")
        idx = self.cmb_lyrics_mode.findData(str(prefs.get("mode") or "triple"))
        self.cmb_lyrics_mode.setCurrentIndex(max(idx, 0))
        self.cmb_lyrics_mode.currentIndexChanged.connect(self._on_lyrics_changed)
        row_mode.addWidget(self._label("显示模式"))
        row_mode.addWidget(self.cmb_lyrics_mode)
        row_mode.addStretch()
        lay.addLayout(row_mode)

        row_lock = QHBoxLayout()
        self.cb_lyrics_locked = QCheckBox("锁定歌词（开启后鼠标穿透、不可拖动）")
        self.cb_lyrics_locked.setChecked(bool(prefs.get("locked", True)))
        self.cb_lyrics_locked.setToolTip("解锁后可按住歌词窗拖动；松手保持解锁，直到再勾选锁定")
        self.cb_lyrics_locked.toggled.connect(self._on_lyrics_changed)
        row_lock.addWidget(self.cb_lyrics_locked)
        row_lock.addStretch()
        lay.addLayout(row_lock)

        row_reset = QHBoxLayout()
        btn_reset_pos = QPushButton("重置位置（回屏幕中央）")
        btn_reset_pos.clicked.connect(self._on_lyrics_reset_pos)
        row_reset.addWidget(btn_reset_pos)
        row_reset.addStretch()
        lay.addLayout(row_reset)

        self.lbl_lyrics_status = QLabel("")
        self.lbl_lyrics_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        lay.addWidget(self.lbl_lyrics_status)
        root.addWidget(box)
        self._lyrics_group_ready = True   # 构造完成，此后 *Changed 才触发保存

    def _collect_lyrics_prefs(self) -> dict:
        return {
            "font": self.cmb_lyrics_font.currentFont().family(),
            "font_size": self.spin_lyrics_size.value(),
            "color": self._lyrics_color.name().upper(),
            "bg_opacity": self.slider_lyrics_opacity.value(),
            "mode": self.cmb_lyrics_mode.currentData() or "triple",
            "locked": self.cb_lyrics_locked.isChecked(),
        }

    def _on_lyrics_changed(self, *_args) -> None:
        # 构造期信号（setCurrentFont/setValue 触发）忽略——控件未就绪且不应写盘
        if not getattr(self, "_lyrics_group_ready", False):
            return
        self._apply_lyrics_prefs(self._collect_lyrics_prefs())

    def _on_lyrics_opacity(self, value: int) -> None:
        self.lbl_lyrics_opacity.setText(f"{value}%")
        self._on_lyrics_changed()

    def _on_lyrics_color(self) -> None:
        color = QColorDialog.getColor(self._lyrics_color, self, "选择歌词颜色")
        if color.isValid():
            self._lyrics_color = color
            self._refresh_lyrics_color_btn()
            self._on_lyrics_changed()

    def _refresh_lyrics_color_btn(self) -> None:
        self.btn_lyrics_color.setStyleSheet(
            f"background-color: {self._lyrics_color.name()}; "
            f"border: 1px solid {Tokens.BORDER}; "
            f"border-radius: {Tokens.RADIUS_SMALL}px;")

    def _apply_lyrics_prefs(self, prefs: dict) -> None:
        ok = save_lyrics_prefs(prefs)
        _notify_lyrics_prefs_changed(prefs)   # 传本次 update：无 pos 键 → 通知剥离 pos（窗口不挪位）
        self.lbl_lyrics_status.setText("已保存（歌词窗已生效）" if ok else "保存失败")

    def _on_lyrics_reset_pos(self) -> None:
        ok = save_lyrics_prefs({"pos_x": None, "pos_y": None})
        _notify_lyrics_prefs_changed({"pos_x": None, "pos_y": None})  # 含 pos 键 → 窗口回中央
        self.lbl_lyrics_status.setText("已重置位置（歌词窗回中央）" if ok else "保存失败")

    # ---------- 🎛️ 内置插件（MusicFree/洛雪）----------

    def _build_plugins_group(self, root: QVBoxLayout) -> None:
        self._js_source = self._find_js_source()
        box = QGroupBox("内置插件 (MusicFree/洛雪)")
        lay = QVBoxLayout(box)
        lay.setSpacing(Tokens.SPACING_SM)  # 组内 8px 网格

        # 插件行容器：直接平铺（不再内嵌 QScrollArea → 双重滚动）。
        # tab 页外层 QScrollArea 负责滚动；插件数通常 ~7 个，平铺可接受。
        self._plugins_container = QWidget()
        self._plugins_layout = QVBoxLayout(self._plugins_container)
        self._plugins_layout.setContentsMargins(Tokens.SPACING_XS, Tokens.SPACING_XS,
                                                Tokens.SPACING_XS, Tokens.SPACING_XS)
        self._plugins_layout.setSpacing(2)   # 更紧凑的行间距
        lay.addWidget(self._plugins_container)

        hint = QLabel("在线搜索自动遍历全部 MusicFree 插件；点「测试」验证单源（搜索+URL+能否播）")
        hint.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        btns = QHBoxLayout()
        self.btn_refresh_plugins = QPushButton("刷新插件")
        self.btn_refresh_plugins.setToolTip("重新扫描插件目录（新丢进的 .js 生效）；刷新会短暂中断搜索")
        self.btn_refresh_plugins.clicked.connect(self._on_refresh_plugins)
        self.btn_check_updates = QPushButton("检查更新")
        self.btn_check_updates.setIcon(lucide_icon("search", 16, Colors.TEXT_PRIMARY))
        self.btn_check_updates.setToolTip("拉取全部 MusicFree 插件更新源，对比版本；有新版的行会显示「更新」按钮")
        self.btn_check_updates.clicked.connect(self._on_check_updates)
        self.lbl_plugin_status = QLabel("")
        self.lbl_plugin_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        btns.addWidget(self.btn_refresh_plugins)
        btns.addWidget(self.btn_check_updates)
        btns.addWidget(self.lbl_plugin_status)
        btns.addStretch()
        lay.addLayout(btns)
        root.addWidget(box)
        self._render_plugins()

    def _find_js_source(self):
        if self._music is None:
            return None
        om = getattr(self._music, "online_manager", None)
        if om is None or not hasattr(om, "get_source"):
            return None
        try:
            return om.get_source("js_bridge")
        except Exception:
            return None

    def _render_plugins(self) -> None:
        # 清空容器布局再重建（与 _render_dirs 同模式；spacer 由 takeAt 直接拿走）
        while self._plugins_layout.count():
            it = self._plugins_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._plugin_rows = {}
        self._test_btns = {}
        self._update_btns = {}
        self._delete_btns = {}
        if self._js_source is None:
            lbl = QLabel("JS 桥接未启用（node 缺失或初始化失败）")
            lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
            self._plugins_layout.addWidget(lbl)
            self.btn_refresh_plugins.setEnabled(False)
            return
        plugins = self._js_source.list_plugins() or []
        if not plugins:
            lbl = QLabel("无插件（目录为空）")
            lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
            self._plugins_layout.addWidget(lbl)
            return
        for p in plugins:
            self._plugins_layout.addWidget(self._make_plugin_row(p))

    def _make_plugin_row(self, p: dict) -> QWidget:
        kind = "MusicFree" if p.get("type") == "musicfree" else "洛雪"
        name = p.get("name", "?")
        if p.get("status") == "ok":
            state = "✅"
        else:
            err = p.get("error") or "加载失败"
            err_short = err if len(err) <= 24 else err[:24] + "…"
            state = f"❌ {err_short}"
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 1, 4, 1)   # 更紧凑的行内边距
        lbl = QLabel(f"[{kind}] {name} {state}")
        lbl.setWordWrap(True)
        if p.get("status") != "ok":
            lbl.setToolTip(f"[{kind}] {name}\n完整错误: {p.get('error') or '加载失败'}")
        # 检查更新结果（合并进行显示：有更新 / 已最新 / 检查失败）
        upd = self._plugin_updates.get(name)
        if upd:
            if upd.get("error"):
                err_short = upd["error"] if len(upd["error"]) <= 16 else upd["error"][:16] + "…"
                lbl.setText(f"[{kind}] {name} {state} | 更新: ✗ {err_short}")
                lbl.setToolTip(f"[{kind}] {name}\n检查更新失败: {upd['error']}")
            elif upd.get("has_update"):
                lbl.setText(f"[{kind}] {name} {state} | 有更新 "
                            f"(v{upd.get('local_version')}→v{upd.get('remote_version')})")
                lbl.setToolTip(f"[{kind}] {name}\n"
                               f"本地 v{upd.get('local_version')} → 远端 v{upd.get('remote_version')}")
            else:
                lbl.setText(f"[{kind}] {name} {state} | 已最新 (v{upd.get('local_version')})")
        h.addWidget(lbl, 1)
        btn_del = QPushButton("删除")
        btn_del.setFixedHeight(24)
        btn_del.setMinimumWidth(56)
        btn_del.clicked.connect(lambda _=False, n=name: self._on_delete_plugin(n))
        h.addWidget(btn_del)
        self._delete_btns[name] = btn_del
        if p.get("status") == "ok" and p.get("type") == "musicfree":
            btn = QPushButton("测试")
            btn.setFixedHeight(24)
            btn.setMinimumWidth(64)   # 最小宽度保底；宽度由内容自适应，不再固定（中文字宽+字体缩放会变）
            btn.clicked.connect(lambda _=False, n=name: self._on_test_plugin(n))
            h.addWidget(btn)
            self._plugin_rows[name] = lbl
            self._test_btns[name] = btn
            # 有更新 → 行尾加「更新」按钮
            if upd and upd.get("has_update"):
                btn_upd = QPushButton("更新")
                btn_upd.setFixedHeight(24)
                btn_upd.setMinimumWidth(64)
                btn_upd.clicked.connect(lambda _=False, n=name: self._on_update_plugin(n))
                h.addWidget(btn_upd)
                self._update_btns[name] = btn_upd
        h.addStretch()   # 行尾 stretch：标签占满、按钮靠右，行宽 = 面板宽
        return row

    def _on_test_plugin(self, name: str) -> None:
        if self._js_source is None:
            return
        lbl = self._plugin_rows.get(name)
        btn = self._test_btns.get(name)
        if lbl is None or btn is None:
            return
        print(f"[Test] 开始测试 {name}")
        lbl.setText(f"[测试中…] {name}")
        btn.setEnabled(False)

        def worker():
            t0 = time.monotonic()
            try:
                tracks = self._js_source.search_plugin(name, "晴天", 3)
                n = len(tracks)
                print(f"[Test] {name} search 完成 {n} 条 耗时{time.monotonic()-t0:.2f}s", flush=True)
                if n == 0:
                    res = f"[MusicFree] {name} | 搜索 ✗(0条)"
                else:
                    url_res = "—"
                    try:
                        r = self._js_source.get_play_url(tracks[0])
                        print(f"[Test] {name} getUrl 完成 耗时{time.monotonic()-t0:.2f}s", flush=True)
                        if r and r.get("url"):
                            url_res = _probe_url(r["url"], r.get("headers"))
                            print(f"[Test] {name} probe 完成 耗时{time.monotonic()-t0:.2f}s", flush=True)
                        else:
                            url_res = "✗(null)"
                    except Exception as e:
                        url_res = f"✗({type(e).__name__})"
                    res = f"[MusicFree] {name} | 搜索 ✓({n}条) | URL {url_res}"
            except Exception as e:
                res = f"[MusicFree] {name} | 测试失败 ({type(e).__name__})"
                print(f"[Test] {name} worker 异常 {type(e).__name__}: {e} 耗时{time.monotonic()-t0:.2f}s", flush=True)
            print(f"[Test] 结果: {name} -> {res}", flush=True)
            self._test_done.emit(name, res)   # Signal 回主线程（worker 线程无 Qt 事件循环）

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, name: str, res: str) -> None:
        print(f"[Test] 更新UI: {name} -> {res}")
        lbl = self._plugin_rows.get(name)
        if lbl is not None:
            lbl.setText(res)
        btn = self._test_btns.get(name)
        if btn is not None:
            btn.setEnabled(True)

    def _on_refresh_plugins(self) -> None:
        if self._js_source is None or self._reloading:
            return
        self._reloading = True
        self.btn_refresh_plugins.setEnabled(False)
        self.btn_refresh_plugins.setText("刷新中…")
        self.lbl_plugin_status.setText("刷新会短暂中断搜索…")

        def worker():
            ok = False
            try:
                ok = self._js_source.reload()
            except Exception:
                ok = False
            try:
                n = len(self._js_source.list_plugins() or [])
            except Exception:
                n = 0
            self._refresh_done.emit(ok, n)   # Signal 回主线程

        threading.Thread(target=worker, daemon=True).start()

    def _on_refresh_done(self, ok: bool, n: int) -> None:
        self._reloading = False
        self.btn_refresh_plugins.setEnabled(True)
        self.btn_refresh_plugins.setText("刷新插件")
        tail = getattr(self, "_delete_result_tail", "")
        if tail:
            self.lbl_plugin_status.setText(tail)
            self._delete_result_tail = ""
            self._render_plugins()
            return
        if ok:
            self.lbl_plugin_status.setText(f"已重载 {n} 个插件")
        else:
            self.lbl_plugin_status.setText(f"刷新超时，已加载 {n} 个")
        self._render_plugins()

    # ---------- 🔍 插件检查更新 ----------

    def _on_check_updates(self) -> None:
        if self._js_source is None or self._checking_updates:
            return
        self._checking_updates = True
        self.btn_check_updates.setEnabled(False)
        self.btn_check_updates.setText("检查中…")
        self.lbl_plugin_status.setText("拉取更新源中…")

        def worker():
            updates = []
            try:
                updates = self._js_source.check_updates(timeout=25.0)
            except Exception as e:
                print(f"[CheckUpdates] worker 异常 {type(e).__name__}: {e}", flush=True)
            n_upd = sum(1 for u in updates if u.get("has_update"))
            msg = f"检查完成：{n_upd} 个有更新 / {len(updates)} 个已检查"
            self._updates_done.emit(msg, updates)   # Signal 回主线程

        threading.Thread(target=worker, daemon=True).start()

    def _on_updates_done(self, msg: str, updates: list) -> None:
        self._checking_updates = False
        self.btn_check_updates.setEnabled(True)
        self.btn_check_updates.setText("检查更新")
        self.btn_check_updates.setIcon(lucide_icon("search", 16, Colors.TEXT_PRIMARY))
        self.lbl_plugin_status.setText(msg)
        by_name = {}
        for u in updates:
            by_name[u.get("name") or u.get("file")] = u
        self._plugin_updates = by_name
        self._render_plugins()

    def _on_update_plugin(self, name: str) -> None:
        """用户点行内「更新」→ 确认框 → 后台备份+替换+reload。"""
        if self._js_source is None:
            return
        upd = self._plugin_updates.get(name)
        if not upd:
            return
        if not glass_question(
            self,
            "确认更新插件",
            f"将更新插件「{name}」\n"
            f"v{upd.get('local_version')} → v{upd.get('remote_version')}\n\n"
            f"更新会替换本地插件文件（旧版备份到 data/plugins_backup/），"
            f"并短暂中断在线搜索。\n确定继续？",
        ):
            return
        self.lbl_plugin_status.setText(f"更新 {name} 中…")
        btn_upd = self._update_btns.get(name)
        if btn_upd is not None:
            btn_upd.setEnabled(False)
            btn_upd.setText("更新中…")

        def worker():
            try:
                result = self._js_source.update_plugin(name)
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            # 更新成功 → 从结果里移除该项（行回到无更新状态，下次「检查更新」重新拉）
            remaining = [u for u in self._plugin_updates.values()
                         if (u.get("name") or u.get("file")) != name]
            self._updates_done.emit(
                f"更新 {name}: {'成功（已重载）' if result.get('ok') else '失败: ' + str(result.get('error'))}",
                remaining,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_delete_plugin(self, name: str) -> None:
        """确认 → 后台删除插件文件 + reload → 回主线程刷新列表。"""
        if self._js_source is None:
            return
        if not glass_question(
            self,
            "确认删除插件",
            f"将删除插件文件「{name}」并重新加载插件列表。\n\n"
            f"删除会短暂中断在线搜索。\n确定继续？",
        ):
            return
        self.lbl_plugin_status.setText(f"删除 {name} 中…")
        btn_del = self._delete_btns.get(name)
        if btn_del is not None:
            btn_del.setEnabled(False)
            btn_del.setText("删除中…")

        def worker():
            try:
                result = self._js_source.delete_plugin(name)
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            self._delete_result_tail = (
                f"删除 {name}: {'成功（已重载）' if result.get('ok') else '失败: ' + str(result.get('error'))}"
            )
            try:
                n = len(self._js_source.list_plugins() or [])
            except Exception:
                n = 0
            self._refresh_done.emit(result.get("ok"), n)   # Signal 回主线程

        threading.Thread(target=worker, daemon=True).start()

    def _on_source_manage(self):
        """打开音源管理对话框（从音乐面板挪到设置页）。"""
        if self._music is None:
            return
        from app.ui.dialogs.source_manage_dialog import SourceManageDialog
        dlg = SourceManageDialog(self, on_sources_changed=self._music.sync_custom_sources)
        dlg.exec()

    def _render_dirs(self) -> None:
        while self._dirs_layout.count():
            it = self._dirs_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if not self._dirs:
            lbl = QLabel("暂无音乐目录，点击「+ 添加目录」添加")
            lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
            self._dirs_layout.addWidget(lbl)
            return
        for i, d in enumerate(self._dirs):
            self._dirs_layout.addWidget(self._make_dir_row(i, d))

    def _make_dir_row(self, idx: int, entry: dict) -> QWidget:
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        cb = QCheckBox("启用")
        cb.setChecked(bool(entry.get("enabled", True)))
        cb.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        cb.toggled.connect(lambda on, i=idx: self._on_toggle_dir(i, on))
        h.addWidget(cb)
        lbl = QLabel(entry.get("path", "?"))
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        h.addWidget(lbl, 1)
        btn_edit = QPushButton("编辑")
        btn_edit.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        btn_edit.setMinimumWidth(56)
        btn_edit.setFixedHeight(28)
        btn_edit.clicked.connect(lambda _=False, i=idx: self._on_edit_dir(i))
        h.addWidget(btn_edit)
        btn_del = QPushButton()
        btn_del.setIcon(lucide_icon("x", 13, Colors.DANGER))
        btn_del.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        btn_del.setMinimumWidth(32)
        btn_del.setFixedHeight(28)
        btn_del.setToolTip("删除此目录")
        btn_del.clicked.connect(lambda _=False, i=idx: self._on_del_dir(i))
        h.addWidget(btn_del)
        return row

    def _save_dirs(self) -> None:
        if save_music_dirs(self._dirs):
            self._render_dirs()

    def _on_toggle_dir(self, idx: int, on: bool) -> None:
        self._dirs[idx]["enabled"] = bool(on)
        self._save_dirs()

    def _on_add_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择音乐目录")
        if not path:
            return
        self._dirs.append({
            "path": path,
            "enabled": True,
            "extensions": list(ALL_EXTENSIONS),
            "exclude_subdirs": False,
        })
        self._save_dirs()

    def _on_edit_dir(self, idx: int) -> None:
        dlg = _DirEditDialog(self._dirs[idx], self)
        if dlg.exec() != QDialog.Accepted:
            return
        entry = dlg.result_entry()
        self._dirs[idx].update(entry)
        self._save_dirs()

    def _on_del_dir(self, idx: int) -> None:
        self._dirs.pop(idx)
        self._save_dirs()

    def _scan_dirs(self) -> list | None:
        """返回启用的目录配置；无启用目录时提示并返回 None。"""
        enabled = [d for d in self._dirs if d.get("enabled")]
        if not enabled:
            self.lbl_scan_status.setText("无启用的音乐目录")
            return None
        return enabled

    def _apply_scan_dirs(self, enabled: list) -> bool:
        """把启用目录应用到 music.scanner（直接赋值，零业务改动）。"""
        if self._music is None:
            return False
        exts = set()
        for d in enabled:
            for e in (d.get("extensions") or []):
                e = str(e).lower().lstrip(".")
                if e:
                    exts.add("." + e)
        if not exts:
            exts = {"." + e for e in ALL_EXTENSIONS}
        try:
            self._music.scanner.scan_dirs = [d["path"] for d in enabled]
            self._music.scanner.extensions = exts
            return True
        except Exception as e:
            print(f"[Settings] 应用扫描目录失败: {e}")
            return False

    def _on_scan_all(self) -> None:
        enabled = self._scan_dirs()
        if enabled is None or not self._apply_scan_dirs(enabled):
            return
        self.lbl_scan_status.setText("扫描中…")
        self.btn_scan_all.setEnabled(False)
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
        self.btn_scan_all.setEnabled(True)
        self.lbl_scan_status.setText(
            f"扫描完成：新增{result.get('added', 0)} 更新{result.get('updated', 0)} "
            f"跳过{result.get('skipped', 0)} 错误{result.get('errors', 0)}")

    def _on_scan_failed(self, err: str) -> None:
        self.btn_scan_all.setEnabled(True)
        self.lbl_scan_status.setText(f"扫描失败: {err}")

    # ---------- 🎤 语音 TTS ----------

    def _build_tts_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("语音 TTS")
        lay = QVBoxLayout(box)
        lay.setSpacing(Tokens.SPACING_SM)  # 组内 8px 网格

        # 引擎下拉（最上；切换联动音色/滑块）
        erow = QHBoxLayout()
        erow.addWidget(self._label("引擎："))
        self.cmb_engine = QComboBox()
        self.cmb_engine.addItem("Qwen（阿里云）", "qwen")
        self.cmb_engine.addItem("Edge（在线）", "edge")
        self.cmb_engine.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        erow.addWidget(self.cmb_engine, 1)
        lay.addLayout(erow)

        # 音色下拉（只选，按引擎填充）
        vrow = QHBoxLayout()
        vrow.addWidget(self._label("音色："))
        self.cmb_voice = QComboBox()
        self.cmb_voice.setEditable(False)
        self.cmb_voice.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        vrow.addWidget(self.cmb_voice, 1)
        lay.addLayout(vrow)

        # 语速滑块（qwen：±100 → 0.5~2.0；edge：-50~+50 → +N%）
        self._rate_slider = NoWheelSlider(Qt.Horizontal)
        self._rate_slider.setValue(0)
        self.lbl_rate = QLabel("语速：")
        self.lbl_rate.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        lay.addWidget(self.lbl_rate)
        lay.addWidget(self._rate_slider)

        # 音量滑块（qwen：0~100 int；edge：0~100 → +N%）
        self._vol_slider = NoWheelSlider(Qt.Horizontal)
        self._vol_slider.setValue(0)
        self.lbl_vol = QLabel("音量：")
        self.lbl_vol.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        lay.addWidget(self.lbl_vol)
        lay.addWidget(self._vol_slider)

        # API Key 行：状态 + 密码输入 + 保存（写 tools/.qwen_key，不碰 settings.yaml）
        krow = QHBoxLayout()
        krow.addWidget(self._label("API Key："))
        self.lbl_key_status = QLabel("")
        self.lbl_key_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        krow.addWidget(self.lbl_key_status)
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.Password)
        self._key_edit.setPlaceholderText("sk-...（仅本地保存 tools/.qwen_key）")
        krow.addWidget(self._key_edit, 1)
        self._btn_key_save = QPushButton("保存")
        krow.addWidget(self._btn_key_save)
        lay.addLayout(krow)

        root.addWidget(box)

        # 从 settings.yaml 加载当前值并接线
        self._load_tts_values()
        self.cmb_engine.currentIndexChanged.connect(self._on_engine_change)
        self.cmb_voice.currentTextChanged.connect(self._on_tts_change)
        self._rate_slider.valueChanged.connect(self._on_tts_change)
        self._vol_slider.valueChanged.connect(self._on_tts_change)
        self._btn_key_save.clicked.connect(self._on_key_save)

    def _fill_voices(self, engine: str, current_vid: str = "") -> None:
        """按引擎填充音色下拉（只选）。blockSignals 防填充过程误触发变更。"""
        self.cmb_voice.blockSignals(True)
        self.cmb_voice.clear()
        if engine == "qwen":
            for vid, desc in _QWEN_VOICES:
                self.cmb_voice.addItem(_voice_label(vid, desc), vid)
        else:
            for vid in _EDGE_VOICES:
                self.cmb_voice.addItem(vid, vid)
        if current_vid:
            i = self.cmb_voice.findData(current_vid)
            if i >= 0:
                self.cmb_voice.setCurrentIndex(i)
            else:
                self.cmb_voice.insertItem(0, _voice_label(current_vid, ""), current_vid)
                self.cmb_voice.setCurrentIndex(0)
        self.cmb_voice.blockSignals(False)

    def _current_voice_id(self) -> str:
        """当前音色 id（下拉 data；兜底取显示文本）。"""
        return self.cmb_voice.currentData() or self.cmb_voice.currentText().strip()

    def _load_tts_values(self) -> None:
        try:
            import yaml
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            tts_cfg = cfg.get("tts", {})
        except Exception:
            return
        engine = tts_cfg.get("engine", "qwen")
        idx = self.cmb_engine.findData(engine)
        # blockSignals：加载期间不触发 _on_engine_change（防止误写配置）
        self.cmb_engine.blockSignals(True)
        self.cmb_engine.setCurrentIndex(max(idx, 0))
        self.cmb_engine.blockSignals(False)
        if engine == "qwen":
            voice = tts_cfg.get("qwen_voice", _QWEN_DEFAULT_VOICE)
            try:
                rate = float(tts_cfg.get("qwen_rate", 1.0))
                volume = int(tts_cfg.get("qwen_volume", 50))
            except Exception:
                rate, volume = 1.0, 50
            self._fill_voices("qwen", voice)
            self._rate_slider.blockSignals(True)
            self._rate_slider.setRange(-100, 100)
            self._rate_slider.setValue(int(round((rate - 1.0) * 100)))
            self._rate_slider.blockSignals(False)
            self._vol_slider.blockSignals(True)
            self._vol_slider.setRange(0, 100)
            self._vol_slider.setValue(volume)
            self._vol_slider.blockSignals(False)
            self.lbl_rate.setText(f"语速：{rate:.2f}（{self._rate_slider.value():+d}%）")
            self.lbl_vol.setText(f"音量：{volume}")
        else:
            voice = tts_cfg.get("edge_voice", _EDGE_DEFAULT_VOICE)
            rate = tts_cfg.get("rate", "+0%")
            volume = tts_cfg.get("volume", "+0%")
            self._fill_voices("edge", voice)
            self._rate_slider.blockSignals(True)
            self._rate_slider.setRange(-50, 50)
            self._rate_slider.setValue(int(str(rate).replace("%", "").replace("+", "")))
            self._rate_slider.blockSignals(False)
            self._vol_slider.blockSignals(True)
            self._vol_slider.setRange(0, 100)
            self._vol_slider.setValue(int(str(volume).replace("%", "").replace("+", "")))
            self._vol_slider.blockSignals(False)
            self.lbl_rate.setText(f"语速：{rate}")
            self.lbl_vol.setText(f"音量：{volume}")
        self._refresh_key_status()

    def _on_engine_change(self, *_args) -> None:
        """切引擎：音色重置为该引擎默认 + 滑块范围/值切换（blockSignals 防误触发）。"""
        engine = self.cmb_engine.currentData() or "qwen"
        if engine == "qwen":
            self._fill_voices("qwen", _QWEN_DEFAULT_VOICE)
            self._rate_slider.blockSignals(True)
            self._rate_slider.setRange(-100, 100)
            self._rate_slider.setValue(0)
            self._rate_slider.blockSignals(False)
            self._vol_slider.blockSignals(True)
            self._vol_slider.setRange(0, 100)
            self._vol_slider.setValue(50)
            self._vol_slider.blockSignals(False)
            self.lbl_rate.setText("语速：1.00（+0%）")
            self.lbl_vol.setText("音量：50")
        else:
            self._fill_voices("edge", _EDGE_DEFAULT_VOICE)
            self._rate_slider.blockSignals(True)
            self._rate_slider.setRange(-50, 50)
            self._rate_slider.setValue(0)
            self._rate_slider.blockSignals(False)
            self._vol_slider.blockSignals(True)
            self._vol_slider.setRange(0, 100)
            self._vol_slider.setValue(0)
            self._vol_slider.blockSignals(False)
            self.lbl_rate.setText("语速：+0%")
            self.lbl_vol.setText("音量：+0%")
        # 保存新引擎默认值 + 立即生效
        self._on_tts_change()

    def _on_tts_change(self, *_args) -> None:
        engine = self.cmb_engine.currentData() or "qwen"
        voice = self._current_voice_id()
        if engine == "qwen":
            rate = _qwen_rate(self._rate_slider.value())
            volume = _qwen_vol(self._vol_slider.value())
            self.lbl_rate.setText(f"语速：{rate:.2f}（{self._rate_slider.value():+d}%）")
            self.lbl_vol.setText(f"音量：{volume}")
            # 持久化到 settings.yaml tts 节（带锁）；Key 不在此文件
            save_settings_yaml({"tts": {
                "engine": "qwen",
                "qwen_voice": voice,
                "qwen_rate": round(rate, 2),
                "qwen_volume": volume,
            }})
        else:
            rate = _edge_pct(self._rate_slider.value())
            volume = _edge_pct(self._vol_slider.value())
            self.lbl_rate.setText(f"语速：{rate}")
            self.lbl_vol.setText(f"音量：{volume}")
            save_settings_yaml({"tts": {
                "engine": "edge",
                "edge_voice": voice,
                "rate": rate,
                "volume": volume,
            }})
        # 立即生效（TTSController setter 按引擎路由，下次生成即用新值）
        if self._tts is not None:
            try:
                self._tts.set_voice(voice)
                self._tts.set_rate(rate)
                self._tts.set_volume(volume)
            except Exception as e:
                print(f"[Settings] TTS 应用失败: {e}")

    def _refresh_key_status(self) -> None:
        """Key 状态：走 TTSController.has_qwen_key()（环境变量 / settings / tools/.qwen_key）。"""
        configured = False
        if self._tts is not None:
            try:
                configured = self._tts.has_qwen_key()
            except Exception:
                configured = False
        if configured:
            self.lbl_key_status.setText("已配置 ✓")
            self.lbl_key_status.setStyleSheet(f"color: {Colors.SUCCESS}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        else:
            self.lbl_key_status.setText("未配置")
            self.lbl_key_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")

    def _on_key_save(self) -> None:
        """保存 API Key 到 tools/.qwen_key（gitignore），不碰 settings.yaml。"""
        key = self._key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "API Key", "请输入 Key（sk-...）")
            return
        import os as _os

        from app.core.path_resolve import PROJECT_ROOT
        # 统一外部读写路径：frozen 下 = exe 同级 tools/.qwen_key（绝不再写 _internal）
        key_path = _os.path.join(str(PROJECT_ROOT), "tools", ".qwen_key")
        try:
            _os.makedirs(_os.path.dirname(key_path), exist_ok=True)
            with open(key_path, "w", encoding="utf-8") as _f:
                _f.write(key + "\n")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"写入 {key_path} 失败: {e}")
            return
        self._key_edit.clear()
        self._refresh_key_status()
        print(f"[Settings] Qwen API Key 已保存（{key_path}）")

    # ---------- 🎮 LoL 开关 ----------

    def _build_lol_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("LoL")
        lay = QVBoxLayout(box)
        lay.setSpacing(Tokens.SPACING_SM)  # 组内 8px 网格

        prefs = load_ui_prefs()
        self.btn_gate = ToggleButton("仅 LoL 前台时触发",
                                     bool(prefs.get("combo_window_gate", True)))
        self.btn_gate.setMinimumWidth(180)
        self.btn_gate.toggled_state.connect(self._on_gate)
        lay.addWidget(self.btn_gate)

        self.btn_equip = ToggleButton("装备提醒",
                                      bool(prefs.get("equip_notify", True)))
        self.btn_equip.setMinimumWidth(180)
        self.btn_equip.toggled_state.connect(self._on_equip)
        lay.addWidget(self.btn_equip)

        # LoL 静态数据一键更新（英雄/技能/装备）
        h_data = QHBoxLayout()
        self.btn_update_data = QPushButton("更新数据（英雄/技能/装备）")
        self.btn_update_data.setIcon(lucide_icon("rotate-ccw", 16, Colors.TEXT_PRIMARY))
        self.btn_update_data.setMinimumWidth(180)
        self.btn_update_data.clicked.connect(self._on_update_data)
        h_data.addWidget(self.btn_update_data)
        self.lbl_update_status = QLabel("")
        self.lbl_update_status.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        self.lbl_update_status.setWordWrap(True)
        h_data.addWidget(self.lbl_update_status, 1)
        lay.addLayout(h_data)

        root.addWidget(box)
        self._data_done.connect(self._on_data_done)

    def _on_gate(self, on: bool) -> None:
        if self._combo is not None:
            self._combo.set_window_gate(on)
        save_ui_prefs({"combo_window_gate": on})

    def _on_equip(self, on: bool) -> None:
        save_ui_prefs({"equip_notify": on})
        if self._equip_toggle is not None:
            self._equip_toggle(on)
        print(f"[Equipment] 装备提醒: {'开' if on else '关'}")  # 对齐 [Combo] 格式

    # ---------- 🎮 LoL 数据更新 ----------

    def _on_update_data(self) -> None:
        """点「更新数据」→ 后台线程拉取英雄/技能/装备（~30-60s），完成经 Signal 回主线程。"""
        self.btn_update_data.setEnabled(False)
        self.btn_update_data.setText("更新中…")

        def worker():
            try:
                stats = _update_lol_data(timeout=10.0)
                msg = (f"已更新：{stats['champions']} 英雄 / {stats['abilities']} 技能 / "
                       f"{stats['items']} 装备（重启后全部生效）")
            except Exception as e:  # noqa: BLE001
                msg = f"更新失败：{e}（旧数据保留）"
            self._data_done.emit(msg)  # 后台线程 emit → Qt 自动 QueuedConnection 投递主线程

        threading.Thread(target=worker, daemon=True).start()

    def _on_data_done(self, msg: str) -> None:
        """主线程槽：恢复按钮 + 显示结果。"""
        self.lbl_update_status.setText(msg)
        self.btn_update_data.setEnabled(True)
        self.btn_update_data.setText("更新数据（英雄/技能/装备）")
        self.btn_update_data.setIcon(lucide_icon("rotate-ccw", 16, Colors.TEXT_PRIMARY))

    # ---------- 🖥️ 界面 ----------

    def _build_ui_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("界面")
        lay = QVBoxLayout(box)
        lay.setSpacing(Tokens.SPACING_SM)  # 组内 8px 网格

        self.btn_overlay = ToggleButton("显示游戏内 Overlay", self._overlay_enabled)
        self.btn_overlay.setMinimumWidth(180)
        self.btn_overlay.toggled_state.connect(self._on_overlay_toggle)
        lay.addWidget(self.btn_overlay)

        irow = QHBoxLayout()
        irow.addWidget(self._label("刷新间隔（毫秒）："))
        self.spin_refresh = QSpinBox()
        self.spin_refresh.setRange(100, 5000)
        self.spin_refresh.setSingleStep(100)
        self.spin_refresh.setValue(int(load_ui_prefs().get("ui_refresh_interval", 500)))
        irow.addWidget(self.spin_refresh)
        irow.addStretch()
        lay.addLayout(irow)
        lay.addWidget(QLabel("影响主界面各面板的定时刷新频率（当前值仅存 ui_prefs.json）。"))

        root.addWidget(box)

        self.spin_refresh.valueChanged.connect(self._handle_refresh_changed)

    def _on_overlay_toggle(self, on: bool) -> None:
        if self._overlay_toggle is not None:
            self._overlay_toggle(on)

    def _build_reset_row(self, root: QVBoxLayout) -> None:
        """设置页底部：恢复默认设置（只重置设置页管理的配置）。"""
        row = QHBoxLayout()
        row.addStretch()
        btn = QPushButton("恢复默认设置")
        btn.setStyleSheet(f"color: {Colors.DANGER};")
        btn.setToolTip("重置界面偏好 / 悬浮窗设置 / 音乐目录 / TTS 音色")
        btn.clicked.connect(self._on_reset_defaults)
        row.addWidget(btn)
        root.addLayout(row)

    def _on_reset_defaults(self) -> None:
        ret = QMessageBox.question(
            self, "恢复默认设置",
            "将重置：界面偏好 / 悬浮窗设置 / 音乐目录 / TTS 音色\n\n"
            "不影响：连招配置 / mpv 路径 / 音源 / 自动化配置\n\n确定继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        reset_ui_prefs()
        reset_music_dirs()
        save_settings_yaml({"tts": {
            "engine": "qwen",
            "qwen_voice": _QWEN_DEFAULT_VOICE,
            "qwen_rate": 1.0,
            "qwen_volume": 50,
        }})
        # 刷新面板 UI
        self._dirs = load_music_dirs()
        self._render_dirs()
        self._load_tts_values()
        prefs = load_ui_prefs()
        self.btn_gate.set_state(bool(prefs.get("combo_window_gate", True)))
        self.btn_equip.set_state(bool(prefs.get("equip_notify", True)))
        if self._overlay_toggle is not None:
            self._overlay_toggle(True)  # 先同步后端（set_enabled(True)）
        self.btn_overlay.set_state(True)  # 再同步 UI（blockSignals，不重复触发回调）
        self.spin_refresh.setValue(int(prefs.get("ui_refresh_interval", 500)))
        if self._on_refresh_changed is not None:
            self._on_refresh_changed(int(prefs.get("ui_refresh_interval", 500)))
        if self._tts is not None:
            try:
                self._tts.set_voice(_QWEN_DEFAULT_VOICE)
                self._tts.set_rate(1.0)
                self._tts.set_volume(50)
            except Exception as e:
                print(f"[Settings] TTS 重置应用失败: {e}")

    def _handle_refresh_changed(self, ms: int) -> None:
        """刷新间隔槽。方法名避开 _on_refresh_changed（实例属性 = 外部回调，
        旧实现被属性遮蔽，on_refresh_changed=None 时构造即崩——已修复）。"""
        save_ui_prefs({"ui_refresh_interval": int(ms)})
        if self._on_refresh_changed is not None:
            self._on_refresh_changed(int(ms))
