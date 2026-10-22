"""电影面板：搜索 + 结果列表 + 播放控制（暂停/继续/进度条/全屏/倍速/音量/静音）
+ 剧集选集 + 自动连播 + Assrt 字幕。"""
import functools
import html as _html_mod
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from PySide6.QtCore import QObject, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from app.core.logger import get_logger
from app.core.path_resolve import PROJECT_ROOT

from ...movie import douban_rank
from ...movie.rank_providers import TgmengProvider, default_provider
from ..dialogs.movie_source_dialog import MovieSourceDialog
from ..dialogs.parse_link_dialog import ParseLinkDialog
from ..widgets.clickable_slider import ClickableSlider
from ..widgets.glass_dialog import GlassDialog
from ..widgets.glass_msg import _GlassMsgBox, glass_info

logger = get_logger()

# ---- 海报缓存（复用 overlay_assets 模式：磁盘缓存 + 模块级内存 dict） ----
_POSTER_DIR = PROJECT_ROOT / "data" / "movie" / "posters"
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


def _make_thumb_pixmap(pix, size=_POSTER_SIZE, radius=8):
    """海报缩略：KeepAspectRatio + SmoothTransformation 缩放（防锯齿）+ 圆角。
    统一 QImage 通道：QPixmap 入参自动 toImage()。海报下载 worker 在后台线程
    （ThreadPoolExecutor）调用，QPainter 只绘制 QImage——QImage 是值语义、
    跨线程安全，杜绝 QPixmap（依赖平台 GUI 资源）跨线程崩溃。"""
    if isinstance(pix, QPixmap):
        pix = pix.toImage()
    if pix.isNull():
        return pix
    scaled = pix.scaled(size[0], size[1], Qt.KeepAspectRatio, Qt.SmoothTransformation)
    out = QImage(size[0], size[1], QImage.Format_ARGB32)
    out.fill(Qt.transparent)
    p = QPainter(out)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size[0], size[1], radius, radius)
    p.setClipPath(path)
    p.drawImage((size[0] - scaled.width()) // 2, (size[1] - scaled.height()) // 2, scaled)
    p.end()
    return out


# 内容无效的海报 URL（豆瓣对无海报条目返回 text/html ~990B 反爬页，非图——
# 实机取证 5/20 张同况，且集中在 img9 域）。会话内不再重试（避免每轮切换标签重复拉无效内容）。
_db_poster_failed: set = set()

# 豆瓣海报多镜像域。取证：img9 域对部分条目返回反爬页（~990B HTML 非图），
# 换 img1 域同一 photo id 100% 成功（5/5，21-38KB JPEG）；延迟重试/完整 UA/代理均无效
# （域级风控，非频率/UA 问题）——故仅多域回退，不降并发不加间隔。
_POSTER_DOMAINS = tuple(f"img{i}" for i in range(1, 10))


def _poster_candidates(url: str) -> list:
    """同一海报的多域候选 URL：原域在首位（多数条目原域直连即成功），失败再依次换域。"""
    m = re.search(r"img(\d)\.doubanio\.com", url or "")
    if not m:
        return [url]
    cur = int(m.group(1))
    order = [cur] + [i for i in range(1, 10) if i != cur]
    return [url.replace(f"img{cur}.doubanio.com", f"img{i}.doubanio.com") for i in order]


class _RankItemDelegate(QStyledItemDelegate):
    """榜单海报 item 自绘（对齐爱奇艺：海报 + 片名大字 + 评分小字 + hover/选中背景）。
    item 数据：UserRole=dict（title/rate/cover/url/id/db_key），DecorationRole=QIcon。"""

    _GRID = (170, 250)
    _PAD = 15          # 海报左右留白
    _TOP = 10          # 海报上留白
    _TITLE_H = 22
    _RATE_H = 16

    def paint(self, painter, option, index):
        data = index.data(Qt.UserRole)
        if not isinstance(data, dict):
            super().paint(painter, option, index)
            return
        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(60, 64, 74))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(option.rect, QColor(46, 49, 58))
        # 海报（icon 已缩略+圆角；AlignCenter 保比例）
        icon = index.data(Qt.DecorationRole)
        img = QRect(option.rect.left() + self._PAD, option.rect.top() + self._TOP,
                    option.rect.width() - self._PAD * 2,
                    option.rect.height() - self._TOP - self._TITLE_H - self._RATE_H - 6)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, img, Qt.AlignCenter)
        # 片名（大字 + 省略号）
        title = str(data.get("title") or "")
        f1 = QFont("Microsoft YaHei", 10, QFont.Bold)
        painter.setFont(f1)
        el = QFontMetrics(f1).elidedText(title, Qt.ElideRight,
                                         option.rect.width() - 12)
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(QRect(option.rect.left() + 6,
                               option.rect.top() + option.rect.height() - self._TITLE_H - self._RATE_H,
                               option.rect.width() - 12, self._TITLE_H),
                         Qt.AlignHCenter | Qt.AlignVCenter, el)
        # 评分（小字角标）
        rate = str(data.get("rate") or "")
        if rate:
            f2 = QFont("Microsoft YaHei", 9)
            painter.setFont(f2)
            painter.setPen(QColor(250, 180, 60))
            painter.drawText(QRect(option.rect.left() + 6,
                                   option.rect.top() + option.rect.height() - self._RATE_H,
                                   option.rect.width() - 12, self._RATE_H),
                             Qt.AlignHCenter | Qt.AlignVCenter, rate)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(*self._GRID)


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

from ...movie.movie_controller import MovieController
from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.icon_utils import lucide_icon


class _PosterWallDelegate(QStyledItemDelegate):
    """在线搜索结果海报墙：海报（圆角缩略）+ 电影名 14 主色 + 备注 12 次色。
    item 数据：UserRole=dict（vod_name/vod_remarks/...），DecorationRole=QIcon。"""

    _GRID = (170, 292)          # 海报 140×210 + 名行 + 备注行
    _POSTER = (140, 210)
    _TOP = 4
    _RADIUS = Tokens.RADIUS_SMALL

    def sizeHint(self, option, index) -> QSize:
        return QSize(*self._GRID)

    def paint(self, painter, option, index):
        data = index.data(Qt.UserRole)
        if not isinstance(data, dict):
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = option.rect
        # hover/选中：微弱背景（不抢海报视觉）
        if option.state & QStyle.State_Selected:
            painter.fillRect(rect, QColor(60, 64, 74))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(rect, QColor(46, 49, 58))
        # 海报（圆角 8 裁剪，居中）
        pw, ph = self._POSTER
        px = rect.left() + (rect.width() - pw) // 2
        py = rect.top() + self._TOP
        icon = index.data(Qt.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            pm = icon.pixmap(pw, ph)
            clip = QPainterPath()
            clip.addRoundedRect(px, py, pw, ph, self._RADIUS, self._RADIUS)
            painter.setClipPath(clip)
            painter.drawPixmap(px, py, pm)
            painter.setClipping(False)
        # 电影名 14 主色（加粗 + 省略号）
        name = str(data.get("vod_name") or "")
        f1 = QFont()
        f1.setPixelSize(Tokens.FONT_SIZE_BODY)
        f1.setBold(True)
        fm1 = QFontMetrics(f1)
        el = fm1.elidedText(name, Qt.ElideRight, rect.width() - 12)
        painter.setFont(f1)
        painter.setPen(QColor(Tokens.TEXT_PRIMARY))
        painter.drawText(px + 6, py + ph + fm1.ascent() + 6, el)
        # 备注 12 次色
        remarks = str(data.get("vod_remarks") or "")
        f2 = QFont()
        f2.setPixelSize(Tokens.FONT_SIZE_SUB)
        fm2 = QFontMetrics(f2)
        el2 = fm2.elidedText(remarks, Qt.ElideRight, rect.width() - 12)
        painter.setFont(f2)
        painter.setPen(QColor(Tokens.TEXT_SECONDARY))
        painter.drawText(px + 6, py + ph + fm1.height() + 6 + fm2.ascent(), el2)
        painter.restore()


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
    """串行下载整批海报到磁盘缓存；逐张 done(vod_id, 本地路径)（失败=""）。
    finished 在整批结束后 emit（面板用它做"待处理批次"补偿）。
    cancel() 置位后循环尽快退出（当前阻塞请求最多等到 TIMEOUT）。"""

    done = Signal(str, str)
    finished = Signal()

    def __init__(self, jobs):
        super().__init__()
        self._jobs = jobs  # [(vod_id, url)]
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for vod_id, url in self._jobs:
            if self._cancelled:
                break
            path = _POSTER_DIR / f"{vod_id}.jpg"
            try:
                if not path.exists():
                    r = requests.get(url, timeout=douban_rank.TIMEOUT)
                    if r.status_code == 200 and r.content:
                        path.write_bytes(r.content)
                if path.exists():
                    self.done.emit(str(vod_id), str(path))
                    continue
            except Exception as e:
                logger.error(f"[Movie] 海报下载失败 {vod_id}: {e}")
            self.done.emit(str(vod_id), "")
        self.finished.emit()


class _DbPosterBatchWorker(QObject):
    """豆瓣榜单海报下载（独立 worker，不碰现有 _PosterBatchWorker 的 job 结构）。
    豆瓣海报 CDN 防盗链：必须带 UA+Referer（裸连/只带 UA 均 418，取证确认）。
    逐张 done(key, 本地路径)（失败=""）；key 形如 "db36179101"。
    内容校验：豆瓣对无海报条目返回 text/html ~990B 反爬页（非图，实机取证）——
    非 JPEG/过小 → 记 _db_poster_failed（会话内不再重试）+ done 空（占位兜底）。
    并发：ThreadPoolExecutor(max_workers=4) 对齐音乐下载器并发模式。
    finished 整批结束 emit；cancel() 置位后 run 直接结束（当前请求最多等到超时）。"""

    done = Signal(str, str)
    finished = Signal()

    def __init__(self, jobs):
        super().__init__()
        self._jobs = jobs  # [(key, url)]
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _download(self, key, url):
        for cand in _poster_candidates(url):  # 原域优先，失败换 img1..img9 其余域（取证有效）
            if self._cancelled:
                return ""
            try:
                r = requests.get(cand, headers=douban_rank.HEADERS, timeout=douban_rank.TIMEOUT)
                if r.status_code == 200 and len(r.content) >= 5000 \
                        and r.content[:2] == b"\xff\xd8":
                    img = QImage()
                    if img.loadFromData(r.content):
                        path = _POSTER_DIR / f"{key}.jpg"
                        thumb = _make_thumb_pixmap(img)
                        if thumb.save(str(path)):
                            return str(path)
            except Exception as e:
                logger.error(f"[Movie] 豆瓣海报下载失败 {key} {cand}: {e}")
        # 全部域无效 → 会话内不再重试（占位兜底）
        _db_poster_failed.add(url)
        return ""

    def run(self):
        if self._cancelled:
            self.finished.emit()
            return
        results = {}
        if self._jobs:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(self._download, k, u): (k, u) for k, u in self._jobs}
                for fut in futs:
                    k, _ = futs[fut]
                    try:
                        results[k] = fut.result()
                    except Exception:
                        results[k] = ""
        for key, path in results.items():
            if self._cancelled:
                break
            self.done.emit(str(key), str(path))
        self.finished.emit()


class _RankTagsWorker(QObject):
    """后台拉榜单标签列表（provider 化；失败降级 failed → 状态栏提示不崩）。"""

    done = Signal(list)
    failed = Signal(str)

    def __init__(self, provider):
        super().__init__()
        self._provider = provider

    def run(self):
        try:
            tags = self._provider.get_tags()
            if not tags:
                self.failed.emit(f"{self._provider.name}标签为空")
            else:
                self.done.emit(tags)
        except Exception as e:
            self.failed.emit(str(e))


class _RankSubjectsWorker(QObject):
    """后台拉某标签的榜单条目（provider 化；失败降级 failed → 状态栏提示不崩）。"""

    done = Signal(list, str)  # (subjects, tag)
    failed = Signal(str)

    def __init__(self, tag, provider, page_start=0, page_limit=20):
        super().__init__()
        self._tag = tag
        self._provider = provider
        self._page_start = page_start
        self._page_limit = page_limit

    def run(self):
        try:
            subs = self._provider.get_subjects(self._tag, page_start=self._page_start,
                                               page_limit=self._page_limit)
            self.done.emit(subs, self._tag)
        except Exception as e:
            self.failed.emit(str(e))


class _ParseLinkWorker(QObject):
    """后台解析视频网页链接（videodl 本地解析库，延迟 import 不拖慢启动）。

    done 信号携带 (title, play_url, display_url)：
      - play_url：可直接交给播放器的地址（videodl 的 download_url 是本地 m3u8 清单
        文件，内容为真实网络 http ts 分段；优先把 raw_data.highest_defns_original
        （真实网络清单文本）落到 %TEMP% 缓存，避免污染项目目录）；
      - display_url：真实网络流地址示例（首个 http ts 链接），供弹窗展示。
    """

    done = Signal(str, str, str)   # (title, play_url, display_url)
    failed = Signal(str)

    def __init__(self, url):
        super().__init__()
        self._url = url

    @staticmethod
    def _extract_real_playlist(info) -> str:
        """提取真实网络 m3u8 清单文本（优先完整清单，回退从底层分段 URL 自建）。"""
        raw = getattr(info, "raw_data", {}) or {}
        m3u8_text = str(raw.get("highest_defns_original") or "").strip()
        if m3u8_text:
            return m3u8_text
        # 回退：normal_ids[].defns.{shd|...}[] .urls 是真实 http 分段（每次解析都有）
        try:
            for n in (raw.get("normal_ids") or []):
                defns = (n or {}).get("defns") or {}
                for _key, segs in defns.items():
                    for seg in (segs or []):
                        urls = (seg or {}).get("urls") or []
                        if urls and str(urls[0]).startswith("http"):
                            lines = ["#EXTM3U"]
                            for u in urls:
                                lines.append("#EXTINF:10,")
                                lines.append(str(u))
                            lines.append("#EXT-X-ENDLIST")
                            return "\n".join(lines)
        except Exception:
            pass
        return ""

    @staticmethod
    def _cache_m3u8(info) -> tuple:
        """把真实网络 m3u8 文本写到 %TEMP%/lol_music_parse/<source>_<id>.m3u8。
        返回 (play_url, display_url)：播放地址 + 首个 http 流示例。"""
        download_url = str(getattr(info, "download_url", "") or "")
        m3u8_text = _ParseLinkWorker._extract_real_playlist(info)
        if m3u8_text:
            source = str(getattr(info, "source", "") or "parse")
            ident = str(getattr(info, "identifier", "") or (hash(m3u8_text) & 0xFFFFFF))
            cache_dir = os.path.join(tempfile.gettempdir(), "lol_music_parse")
            try:
                os.makedirs(cache_dir, exist_ok=True)
                play_path = os.path.join(cache_dir, f"{source}_{ident}.m3u8")
                with open(play_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(m3u8_text)
                # 展示用真实网络流：取清单第一条 http 链接
                display = download_url
                for line in m3u8_text.splitlines():
                    line = line.strip()
                    if line.startswith("http"):
                        display = line
                        break
                return play_path, display
            except Exception:
                pass
        # 降级：用库返回的本地清单文件（内容同样为真实 http 分段）
        return download_url, download_url

    def run(self):
        try:
            from videodl import (
                videodl,  # 延迟 import；库 bug：默认参数 None 会崩，必须传 []
            )
            client = videodl.VideoClient(allowed_video_sources=[])
            infos = client.parsefromurl(self._url)
            if infos and getattr(infos[0], "download_url", None):
                play_url, display_url = self._cache_m3u8(infos[0])
                self.done.emit(str(getattr(infos[0], "title", "") or ""),
                               play_url, display_url)
            else:
                self.failed.emit("未返回任何流信息")
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class MoviePanel(QWidget):
    # 语音"开始播放"接管请求——跨线程安全：语音命令在工作线程 execute_async，
    # QTimer.singleShot 从工作线程排队会永不执行（无事件循环，根因）；经信号
    # QueuedConnection 投递到本窗口所在线程（主线程）后再延后接管。
    _voice_attach_requested = Signal()
    # 语音反馈跨线程桥：语音 worker 线程 → 本信号（QueuedConnection）→ 主线程槽
    _voice_msg_signal = Signal(str)
    # 链接解析结果转发（worker 在子线程 emit → Python callable 槽默认 Direct 同线程执行，
    # 弹窗/UI 必须在主线程 → 经本窗口信号 QueuedConnection 转投主线程）。
    _parse_done = Signal(str, str, str)    # (title, play_url, display_url)
    _parse_failed = Signal(str)

    def __init__(self, movie=None, music=None, parent=None):
        super().__init__(parent)
        self._movie = movie if movie is not None else MovieController(music=music)
        # P1：语音搜电影结果 → 状态栏（controller 构造自注册 MOVIE_* 命令，回调在面板侧）
        try:
            self._movie._voice_feedback = self._on_voice_feedback
        except Exception:
            pass
        self._voice_attach_requested.connect(self._on_voice_attach_requested)
        self._voice_msg_signal.connect(self._on_voice_msg)
        self._parse_done.connect(self._on_parse_done)
        self._parse_failed.connect(self._on_parse_failed)
        self._thread = None
        self._worker = None
        self._items_by_row = []
        # 榜单 QThread：同一时刻只持一个引用，运行中绝不覆盖（QThread 崩溃教训）
        self._rank_thread = None
        self._rank_worker = None
        self._rank_tag = None
        # 多平台榜单源（默认豆瓣，体验不变）
        self._rank_provider = default_provider()
        # 海报下载线程：同款防重入 + 待处理补偿（旧线程结束后自动补最新一批）
        self._poster_thread = None
        self._poster_worker = None
        self._rank_db_thread = None
        self._rank_db_worker = None
        self._pending_poster_jobs = None
        self._pending_db_jobs = None
        # 自动字幕 QThread（防重入）：同一时刻只持一个引用，运行中绝不覆盖
        self._sub_thread = None
        self._sub_worker = None
        # 链接解析线程（防重入：同一时刻只持一个引用，运行中忽略新任务）
        self._parse_thread = None
        self._parse_worker = None

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
        self.btn_rank = QPushButton("榜单")
        self.btn_rank.setToolTip("豆瓣榜单（热门/最新/高分…）——点条目按片名搜源内播放")
        search_row.addWidget(self.btn_rank)
        self.btn_source = QPushButton("源")
        self.btn_source.setToolTip("电影源管理（多源 / 测源 / 增删 / 设当前）")
        search_row.addWidget(self.btn_source)
        self.btn_parse_link = QPushButton("🔗 链接解析")
        self.btn_parse_link.setToolTip("粘贴视频网页链接（爱奇艺/腾讯等）→ 后台解析出 m3u8 地址")
        search_row.addWidget(self.btn_parse_link)
        root.addLayout(search_row)

        # 状态行：搜索状态 + 当前源
        status_row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        self.lbl_source = QLabel("")
        self.lbl_source.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        status_row.addWidget(self.lbl_status, 1)
        status_row.addWidget(self.lbl_source)
        root.addLayout(status_row)

        # 结果区：列表/海报 双视图（QStackedWidget，默认海报墙）
        self.stack_results = QStackedWidget()
        self.list_results = QListWidget()
        self.list_posters = QListWidget()
        self.list_posters.setViewMode(QListWidget.IconMode)
        self.list_posters.setIconSize(QSize(*_POSTER_SIZE))
        self.list_posters.setGridSize(QSize(*_PosterWallDelegate._GRID))
        self.list_posters.setResizeMode(QListView.Adjust)
        self.list_posters.setSpacing(Tokens.SPACING_MD)
        self.list_posters.setWordWrap(True)
        self.list_posters.setItemDelegate(_PosterWallDelegate(self.list_posters))
        self.list_posters.setMouseTracking(True)
        self.stack_results.addWidget(self.list_results)
        self.stack_results.addWidget(self.list_posters)
        # 榜单视图（第 3 页）：左上平台选择 + 左标签竖列 + 右海报网格（对齐豆瓣 App 榜单页）
        self.page_rank = QWidget()
        rank_lay = QHBoxLayout(self.page_rank)
        rank_lay.setContentsMargins(0, 0, 0, 0)
        rank_lay.setSpacing(Tokens.SPACING_SM)
        rank_left = QVBoxLayout()
        rank_left.setContentsMargins(0, 0, 0, 0)
        rank_left.setSpacing(Tokens.SPACING_SM)
        self.combo_rank_platform = QComboBox()
        _dp = default_provider()
        self.combo_rank_platform.addItem(_dp.name, _dp)
        _tp = TgmengProvider()
        self.combo_rank_platform.addItem(_tp.name, _tp)
        self.combo_rank_platform.setToolTip("榜单平台（豆瓣默认）")
        self.list_rank_tags = QListWidget()
        self.list_rank_tags.setFixedWidth(96)
        self.list_rank_tags.setToolTip("榜单标签")
        rank_left.addWidget(self.combo_rank_platform)
        rank_left.addWidget(self.list_rank_tags)
        self.list_rank = QListWidget()
        self.list_rank.setViewMode(QListWidget.IconMode)
        self.list_rank.setIconSize(QSize(*_POSTER_SIZE))
        self.list_rank.setGridSize(QSize(170, 250))
        self.list_rank.setResizeMode(QListView.Adjust)
        self.list_rank.setSpacing(Tokens.SPACING_MD)
        self.list_rank.setWordWrap(True)
        self.list_rank.setItemDelegate(_RankItemDelegate(self.list_rank))
        self.list_rank.setToolTip("双击条目 → 按片名搜源内并显示结果（不自动播）")
        rank_lay.addLayout(rank_left)
        rank_lay.addWidget(self.list_rank, 1)
        self.stack_results.addWidget(self.page_rank)
        root.addWidget(self.stack_results, 1)
        self._poster_mode = True
        self.stack_results.setCurrentWidget(self.list_posters)  # 默认海报墙

        # 当前播放
        self.lbl_now = QLabel("未播放")
        self.lbl_now.setAlignment(Qt.AlignCenter)
        root.addWidget(self.lbl_now)

        # 进度条（外部进度条无实际用途——内嵌播放器自带进度，直接隐藏）
        self.slider_pos = ClickableSlider(Qt.Horizontal)
        self.slider_pos.setRange(0, 1000)
        self.slider_pos.setEnabled(False)
        self.slider_pos.hide()   # 从布局移除：不再显示空蓝条
        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setAlignment(Qt.AlignCenter)
        try:  # tabular figures：计时数字等宽不跳动（impeccable）
            _f = self.lbl_time.font()
            _f.setFeature(QFont.Tag("tnum"), 1)
            self.lbl_time.setFont(_f)
        except Exception:
            pass
        self.lbl_res = QLabel("")
        self.lbl_res.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        time_row = QHBoxLayout()
        time_row.addWidget(self.lbl_time, 1)
        time_row.addWidget(self.lbl_res)
        root.addLayout(time_row)

        # 播放控制
        row = QHBoxLayout()
        self.btn_toggle = QPushButton()
        self.btn_toggle.setObjectName("btn_toggle")   # 复用主按钮金字塔样式
        self.btn_toggle.setIcon(lucide_icon("play", 20, Colors.TEXT_PRIMARY))
        self.btn_toggle.setToolTip("播放/暂停")
        self.btn_toggle.setFixedSize(52, 40)   # 主按钮：放大 + 固定尺寸防图标挤压
        self.btn_stop = QPushButton()
        self.btn_stop.setObjectName("btn_stop")       # 复用次按钮尺寸（防挤压）
        self.btn_stop.setIcon(lucide_icon("square", 18, Colors.TEXT_PRIMARY))
        self.btn_stop.setToolTip("停止")
        self.btn_stop.setFixedSize(40, 32)
        row.addWidget(self.btn_toggle)
        row.addWidget(self.btn_stop)
        row.addStretch()
        root.addLayout(row)

        # 增强控制行（--osc=no 后 mpv 窗口无控件，控制全在面板）
        row2 = QHBoxLayout()
        self.btn_fullscreen = QPushButton("全屏")
        self.btn_fullscreen.setIcon(lucide_icon("maximize", 16, Colors.TEXT_PRIMARY))
        self.btn_speed = QPushButton("倍速 1x")
        self.lbl_vol = QLabel("音量")
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(70)
        self.slider_vol.setFixedWidth(120)
        self.btn_mute = QPushButton()
        self.btn_mute.setIcon(lucide_icon("volume-2", 16, Colors.TEXT_PRIMARY))
        self.btn_mute.setToolTip("静音切换")
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

        # 底部控制行整体裁撤：内嵌播放器自带 OSC（播放/停止/全屏/倍速/音量/字幕/连播/快捷键），
        # 外部控制不再显示——仅 hide（保留对象与信号，防业务/测试引用崩）。
        for _w in (self.btn_toggle, self.btn_stop, self.btn_fullscreen, self.btn_speed,
                   self.lbl_vol, self.slider_vol, self.btn_mute, self.btn_subtitle,
                   self.cb_autonext, self.btn_shortcut):
            _w.hide()

        # 倍速档位（B站/爱奇艺标准 9 档；点按钮弹菜单选档，非循环点击）
        self._speed_steps = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
        self._speed_idx = 2  # 1.0x 为默认档

        # 信号
        self.btn_search.clicked.connect(self._do_search)
        self.btn_clear.clicked.connect(self._do_clear)
        self.edit_kw.returnPressed.connect(self._do_search)
        self.btn_rank.clicked.connect(self._enter_rank_view)
        self.combo_rank_platform.currentIndexChanged.connect(self._on_rank_platform_changed)
        self.list_rank_tags.itemClicked.connect(self._on_rank_tag_clicked)
        self.list_rank.itemDoubleClicked.connect(self._on_rank_item_activated)
        self.list_results.itemDoubleClicked.connect(self._on_item_activated)
        self.list_results.itemClicked.connect(self._on_item_clicked)
        self.list_posters.itemClicked.connect(self._on_item_clicked)
        self.list_posters.itemDoubleClicked.connect(self._on_item_activated)
        self.btn_view.clicked.connect(self._on_toggle_view)
        self.btn_source.clicked.connect(self._on_source_manage)
        self.btn_parse_link.clicked.connect(self._on_parse_link)
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
        # 海报磁盘缓存上限清理（启动时一次；超 500 张删最旧）
        self._cleanup_poster_cache()

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

        # 内嵌播放（S0+S1）：懒建隐藏播放窗口，winId 立即注入控制器——
        # 任何播放路径（UI/语音/选集）都走 --wid 内嵌；embed 关则不建（独立窗口现状）。
        self._embed_playing = False
        self._video_win = None
        self._ensure_video_win()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.stop()

    def _ensure_video_win(self):
        """内嵌播放窗口懒建（embed 开 + 未建时创建；__init__ 与语音路径共用兜底）。
        语音"开始播放"接管不再依赖窗口恰好在 __init__ 建好——任何时机首次触发都可补建。"""
        if self._video_win is not None:
            return
        try:
            if self._movie.embed_enabled():
                from ..widgets.video_player_window import VideoPlayerWindow
                self._video_win = VideoPlayerWindow(self._movie)
                self._video_win.stopped.connect(self._on_video_stopped)
                self._movie.set_embed_wid(self._video_win.container.native_id())
        except Exception as e:
            logger.error(f"[Movie] 内嵌播放窗口初始化失败（回退独立窗口）: {e}")
            self._video_win = None
            self._embed_playing = False

    def _on_app_quit(self):
        try:
            # 退出清理（QThread 崩溃修复）：先 cancel（循环快速停）→ 对仍运行的
            # 线程统一 quit() + requestInterruption()（请求中断）→ 统一 wait(2000) 有界等待。
            # 绝不串行 9000：最坏情况网络请求阻塞由 requests 8s 超时收尾，退出不等它；
            # 逐个 9s 串行等待会让退出流程卡死数秒甚至触发二次崩溃。
            for w in (self._worker, self._rank_worker, self._poster_worker,
                      self._rank_db_worker, self._sub_worker):
                cancel = getattr(w, "cancel", None)
                if cancel is not None:
                    try:
                        cancel()
                    except Exception:
                        pass
            threads = [t for t in (self._thread, self._rank_thread, self._poster_thread,
                                   self._rank_db_thread, self._parse_thread, self._sub_thread)
                       if t is not None and t.isRunning()]
            for t in threads:
                t.quit()
                try:
                    t.requestInterruption()
                except Exception:
                    pass
            for t in threads:
                t.wait(2000)
            self._movie.close()
        except Exception:
            pass

    # ---- 搜索 ----

    def _do_search(self):
        # 防重入：搜索线程仍在运行则忽略本次（QThread 同一时刻只持一个引用）
        if self._thread is not None and self._thread.isRunning():
            return
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

    # ---- 榜单视图（豆瓣榜单 + 苹果 CMS 播放对接）----

    def _enter_rank_view(self):
        self.stack_results.setCurrentWidget(self.page_rank)
        if self.list_rank_tags.count() == 0:
            self._load_rank_tags()

    def _load_rank_tags(self):
        """拉当前 provider 的榜单标签；完成后自动拉第一个标签的榜单。防重入（运行中忽略）。"""
        if self._rank_thread is not None and self._rank_thread.isRunning():
            return
        self.lbl_status.setText(f"加载榜单标签（{self._rank_provider.name}）…")
        try:
            self._rank_thread = QThread()
            self._rank_worker = _RankTagsWorker(self._rank_provider)
            self._rank_worker.moveToThread(self._rank_thread)
            self._rank_thread.started.connect(self._rank_worker.run)
            self._rank_worker.done.connect(self._on_rank_tags_done)
            self._rank_worker.failed.connect(self._on_rank_tags_failed)
            self._rank_worker.done.connect(self._rank_thread.quit)
            self._rank_worker.failed.connect(self._rank_thread.quit)
            self._rank_thread.start()
        except Exception as e:
            self.lbl_status.setText(f"榜单加载失败: {e}")

    def _on_rank_platform_changed(self, index: int):
        """平台切换：换 provider → 清空标签/海报 → 重载标签。"""
        if index < 0:
            return
        provider = self.combo_rank_platform.itemData(index)
        if provider is None:
            return
        self._rank_provider = provider
        self._rank_tag = None
        self.list_rank_tags.clear()
        self.list_rank.clear()
        self._load_rank_tags()

    def _on_rank_tags_done(self, tags: list):
        self.list_rank_tags.clear()
        self.list_rank.clear()
        for t in tags:
            item = QListWidgetItem(str(t))
            item.setData(Qt.UserRole, str(t))
            self.list_rank_tags.addItem(item)
        if tags:
            self.list_rank_tags.setCurrentRow(0)
            # 时序：旧 _rank_thread 的 quit 此刻可能仍在排队（isRunning 仍 True）——
            # 直接调 _load_rank_subjects 会被防重入忽略（首次进榜单不自动加载）。
            # 延后一拍等旧线程真正结束再触发默认"热门"标签。
            QTimer.singleShot(0, lambda: self._load_rank_subjects(str(tags[0])))
        else:
            self.lbl_status.setText("榜单标签为空")

    def _on_rank_tags_failed(self, err: str):
        self.lbl_status.setText(f"榜单加载失败: {err}")

    def _on_rank_tag_clicked(self, item):
        tag = item.data(Qt.UserRole)
        if tag and tag != self._rank_tag:
            self._load_rank_subjects(str(tag))

    def _load_rank_subjects(self, tag):
        """拉某标签的榜单条目 → 渲染海报网格。防重入（运行中忽略，连点不崩）。"""
        if self._rank_thread is not None and self._rank_thread.isRunning():
            return
        self._rank_tag = tag
        self.lbl_status.setText(f"加载榜单「{tag}」…")
        try:
            self._rank_thread = QThread()
            self._rank_worker = _RankSubjectsWorker(tag, self._rank_provider, 0, 20)
            self._rank_worker.moveToThread(self._rank_thread)
            self._rank_thread.started.connect(self._rank_worker.run)
            self._rank_worker.done.connect(self._on_rank_subjects_done)
            self._rank_worker.failed.connect(self._on_rank_subjects_failed)
            self._rank_worker.done.connect(self._rank_thread.quit)
            self._rank_worker.failed.connect(self._rank_thread.quit)
            self._rank_thread.start()
        except Exception as e:
            self.lbl_status.setText(f"榜单加载失败: {e}")

    def _on_rank_subjects_done(self, subs: list, tag: str):
        self.list_rank.clear()
        jobs = []
        for s in subs:
            if not isinstance(s, dict):
                continue
            key = "db" + str(s.get("id") or "")
            pit = QListWidgetItem()
            pit.setData(Qt.UserRole, {**s, "db_key": key})
            tip = s.get("title") or ""
            if s.get("rate"):
                tip += f"  评分 {s['rate']}"
            pit.setToolTip(tip)
            pit.setTextAlignment(Qt.AlignHCenter | Qt.AlignBottom)
            self.list_rank.addItem(pit)
            pix = _get_poster(key)
            if pix is not None:
                pit.setIcon(QIcon(QPixmap.fromImage(_make_thumb_pixmap(pix))))
            else:
                # 占位先行（thumb 化圆角），下载完成后替换
                pit.setIcon(QIcon(QPixmap.fromImage(_make_thumb_pixmap(
                    _make_poster_placeholder(s.get("title") or "")))))
                if s.get("cover") and s["cover"] not in _db_poster_failed:
                    jobs.append((key, s["cover"]))
        if jobs:
            self._start_db_poster_jobs(jobs)
        n = len(subs)
        self.lbl_status.setText(f"榜单「{tag}」{n} 部" if n else f"榜单「{tag}」为空")

    def _on_rank_subjects_failed(self, err: str):
        self.lbl_status.setText(f"榜单加载失败: {err}")

    def _on_rank_item_activated(self, item):
        """双击榜单条目 → 按片名搜苹果 CMS → 切回搜索视图显示结果（不自动播，规避误命中）。"""
        data = item.data(Qt.UserRole)
        if not isinstance(data, dict):
            return
        title = (data.get("title") or "").strip()
        if not title:
            return
        self.edit_kw.setText(title)
        self.stack_results.setCurrentWidget(self.list_posters)
        self._do_search()

    def _start_db_poster_jobs(self, jobs):
        """豆瓣榜单海报下载。防重入：旧线程运行中 → 记最新一批待处理，
        旧线程 finished 后自动补下载（不覆盖运行中引用 → 不崩）。"""
        if not jobs:
            return
        if self._rank_db_thread is not None and self._rank_db_thread.isRunning():
            self._pending_db_jobs = jobs
            return
        self._pending_db_jobs = None
        try:
            self._rank_db_thread = QThread()
            self._rank_db_worker = _DbPosterBatchWorker(jobs)
            self._rank_db_worker.moveToThread(self._rank_db_thread)
            self._rank_db_thread.started.connect(self._rank_db_worker.run)
            self._rank_db_worker.done.connect(self._on_poster_done)
            self._rank_db_worker.done.connect(self._rank_db_thread.quit)
            self._rank_db_worker.finished.connect(self._on_db_poster_batch_finished)
            self._rank_db_thread.start()
        except Exception as e:
            logger.error(f"[Movie] 榜单海报下载线程启动失败: {e}")

    def _on_db_poster_batch_finished(self):
        if self._pending_db_jobs:
            jobs = self._pending_db_jobs
            self._pending_db_jobs = None
            QTimer.singleShot(0, lambda: self._start_db_poster_jobs(jobs))

    def _on_voice_feedback(self, msg):
        """语音搜电影结果反馈 → 状态栏（未找到/只有1集/越界/开始播放）。
        语音命令在工作线程 execute_async 直接回调本方法：**绝不在此直接刷 UI**，
        setText 经 _voice_msg_signal（QueuedConnection）投递主线程槽 _on_voice_msg。
        窗口接管/懒建等 UI 动作同样经 _voice_attach_requested 信号投递主线程。"""
        if msg:
            self._voice_msg_signal.emit(str(msg))
            hit = str(msg).startswith("开始播放")
            embed = False
            try:
                embed = bool(self._movie.embed_enabled())
            except Exception:
                pass
            if hit and embed:
                self._voice_attach_requested.emit()

    def _on_voice_msg(self, msg):
        """主线程槽：语音反馈实际刷新状态栏（仅主线程允许直接碰 QWidget）。"""
        self.lbl_status.setText(str(msg))

    def _on_voice_attach_requested(self):
        """主线程收到语音接管请求：懒建窗口 + 延后一拍执行接管（同手动 _do_embed_play）。"""
        try:
            self._ensure_video_win()
            if self._video_win is not None:
                QTimer.singleShot(0, self._do_embed_attach)
        except Exception:
            pass

    def _do_embed_attach(self):
        """语音路径延后一拍：显示窗口 + 接管已开始的播放（P0 卡死修复回调）。
        窗口置顶同 _do_embed_play：SetWindowPos(HWND_TOPMOST) 异步，不激活。"""
        try:
            if self._video_win is None:
                self._ensure_video_win()
            if self._video_win is None:
                return
            self._video_win.show()
            QTimer.singleShot(0, self._video_win.raise_topmost)
            self._video_win.attach_current()
            self._embed_playing = True
        except Exception as e:
            logger.error(f"[Movie] 语音窗口接管异常: {e}")

    def _current_source_name(self):
        """当前源名（管理器读不到/非真实源时返回 None，不打扰 UI）。"""
        try:
            movie = getattr(self, "_movie", None)
            if movie is None:
                return None
            mgr = getattr(movie, "_source_manager", None)
            if mgr is None:
                return None
            name = mgr.get_active_name()
            return name if isinstance(name, str) and name else None
        except Exception:
            return None

    def _refresh_source_label(self):
        """状态栏显示当前源名；读不到时显示“未选择”。"""
        try:
            name = self._current_source_name()
            self.lbl_source.setText(f"当前源：{name}" if name else "当前源：未选择")
        except Exception:
            self.lbl_source.setText("当前源：未选择")

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

    # ---- 链接解析（第一/二阶段：入口 + 后台解析 + 结果弹窗 + 一键播放）----

    def _on_parse_link(self):
        """「链接解析」→ 弹输入框 → 后台 videodl 解析 → 结果弹窗。

        用户填了「自定义解析接口」时跳过 videodl：直接用「接口 + 视频链接」交给播放器。
        """
        dlg = ParseLinkDialog(self)
        if dlg.exec() != ParseLinkDialog.Accepted:
            return
        url = dlg.url()
        if not url.strip():
            glass_info(self, "链接为空", "请输入视频网页链接后再解析。")
            return
        api = dlg.third_party_api()
        if api.strip():
            # 第三方接口备用方案：接口地址 + 视频链接，直接播放
            full = api.strip() + url
            self.lbl_status.setText("使用第三方接口播放…")
            self._play_parsed_url(full, title="第三方接口")
            return
        if self._parse_thread is not None and self._parse_thread.isRunning():
            glass_info(self, "解析中…", "已有解析任务在后台运行，请稍候。")
            return
        self.lbl_status.setText("正在解析链接…（后台）")
        try:
            self._parse_thread = QThread()
            self._parse_worker = _ParseLinkWorker(url)
            self._parse_worker.moveToThread(self._parse_thread)
            self._parse_thread.started.connect(self._parse_worker.run)
            # worker 信号 → 本窗口转发信号（QueuedConnection 回主线程）→ 弹窗/播放
            self._parse_worker.done.connect(self._parse_done)
            self._parse_worker.failed.connect(self._parse_failed)
            self._parse_worker.done.connect(self._parse_thread.quit)
            self._parse_worker.failed.connect(self._parse_thread.quit)
            self._parse_thread.start()
        except Exception as e:
            self.lbl_status.setText(f"解析启动失败: {e}")

    def _play_parsed_url(self, url: str, title: str = "解析流"):
        """直接调现有电影 mpv 实例播放（不经过 movie_controller 的 SGP/资源站逻辑）。"""
        try:
            mpv = self._movie.mpv
            alive = bool(getattr(mpv, "_process", None) and mpv._process.poll() is None)
            if not alive:
                if not mpv.start(taskkill=False, pipe_timeout=5.0, prop_timeout=0.5):
                    raise RuntimeError("mpv 启动失败（未找到 mpv 可执行文件）")
            mpv.loadfile(url)
            mpv.play()
            self.lbl_status.setText(f"正在播放：{title}")
            self.lbl_status.setToolTip(url)
        except Exception as e:
            self.lbl_status.setText("播放失败")
            glass_info(self, "播放失败", f"{type(e).__name__}: {e}")

    def _on_parse_done(self, title: str, play_url: str, display_url: str):
        self.lbl_status.setText("链接解析成功")
        self.lbl_status.setToolTip(play_url)
        box = _GlassMsgBox(self, "解析成功",
                           f"标题：{title or '未知'}\n\n真实网络流：\n{display_url}\n\n"
                           f"播放地址：\n{play_url}",
                           [("确定", False), ("▶ 立即播放", True)])
        box.exec()
        if box._result == "▶ 立即播放":
            self._play_parsed_url(play_url, title=title or "解析流")

    def _on_parse_failed(self, err: str):
        self.lbl_status.setText("链接解析失败")
        glass_info(self, "解析失败", f"请检查链接或稍后再试。\n\n{err}")

    def _start_poster_jobs(self, jobs):
        """搜索海报墙下载。防重入（存量同款缺陷修复）：旧线程运行中 →
        记最新一批待处理，finished 后自动补下载（不覆盖运行中引用 → 不崩）。"""
        if not jobs:
            return
        if self._poster_thread is not None and self._poster_thread.isRunning():
            self._pending_poster_jobs = jobs
            return
        self._pending_poster_jobs = None
        try:
            self._poster_thread = QThread()
            self._poster_worker = _PosterBatchWorker(jobs)
            self._poster_worker.moveToThread(self._poster_thread)
            self._poster_thread.started.connect(self._poster_worker.run)
            self._poster_worker.done.connect(self._on_poster_done)
            self._poster_worker.done.connect(self._poster_thread.quit)
            self._poster_worker.finished.connect(self._on_poster_batch_finished)
            self._poster_thread.start()
        except Exception as e:
            logger.error(f"[Movie] 海报下载线程启动失败: {e}")

    def _on_poster_batch_finished(self):
        if self._pending_poster_jobs:
            jobs = self._pending_poster_jobs
            self._pending_poster_jobs = None
            QTimer.singleShot(0, lambda: self._start_poster_jobs(jobs))

    def _on_poster_done(self, vod_id, path):
        if not path:
            return
        pix = QPixmap(path)
        if pix.isNull():
            return
        _posters_cache[vod_id] = pix
        # 搜索海报墙（vod_id，原图）与榜单海报墙（db_key，缩略+圆角）都匹配更新
        for lst in (self.list_posters, self.list_rank):
            for i in range(lst.count()):
                it = lst.item(i)
                data = it.data(Qt.UserRole)
                if not isinstance(data, dict):
                    continue
                key = str(data.get("vod_id", "")) or str(data.get("db_key", ""))
                if key == vod_id:
                    # 榜单列需缩略（QImage）；QIcon 在本 PySide6 版本不接受裸 QImage，
                    # 主线程（本槽）内 QPixmap.fromImage 转回再设图标——跨线程安全由
                    # _make_thumb_pixmap 的 QImage 通道保证。
                    icon_pix = (QPixmap.fromImage(_make_thumb_pixmap(pix))
                                if lst is self.list_rank else pix)
                    it.setIcon(QIcon(icon_pix))
                    break

    def _play_item(self, it, index=None):
        """统一播放入口：控制器播放 + 状态刷新 + 自动字幕（配置开时每片一次）。
        内嵌模式 → 播放窗口接管（呈现 + 轮询 + 控制）；非 embed → 现状面板直控。"""
        try:
            if self._video_win is not None:
                # P0 卡死修复（宿主窗口系统同步高危）：显示序列延后一拍——
                # 删 raise_()/activateWindow()（show 已置顶；前台锁宿主无效），
                # show+play 挪进 QTimer.singleShot(0) 让当前 slot 先返回（宿主消息泵喘息）。
                QTimer.singleShot(0, functools.partial(self._do_embed_play, it, index))
                return
            ok = self._movie.play(it, index=index)
            if ok:
                name = it.get("vod_name", "") or ""
                self.lbl_now.setText(f"播放中: {name}")
                self.lbl_status.setText("")
                self.slider_pos.setEnabled(True)
                self._sync_toggle_icon()
                self._maybe_auto_subtitle(it)
            else:
                self.lbl_status.setText("无法播放：无可用地址")
        except Exception as e:
            self.lbl_status.setText(f"播放失败: {e}")

    def _do_embed_play(self, it, index=None):
        """embed 延后一拍执行：显示窗口 + 控制器播放 + 状态刷新（P0 卡死修复回调）。
        原同步序列 show→raise_→activateWindow 在宿主窗口系统同步卡死（用户实机两次）；
        show 进 singleShot 回调——当前 slot 已先返回。窗口置顶用 SetWindowPos(HWND_TOPMOST)
        异步恢复（Qt raise_ 同步仍是卡死源；不恢复 activateWindow——宿主前台锁无效）。"""
        try:
            self._video_win.show()
            # 宿主盖窗口修复：置顶异步执行（只动 z-order 不碰前台锁，宿主喘息后置顶）
            QTimer.singleShot(0, self._video_win.raise_topmost)
            ok = self._video_win.play(it, index=index)
            if ok:
                name = it.get("vod_name", "") or ""
                self.lbl_now.setText(f"播放中: {name}")
                self.lbl_status.setText("")
                self._embed_playing = True
                self._sync_toggle_icon()
                self._maybe_auto_subtitle(it)
            else:
                self.lbl_status.setText("无法播放：无可用地址")
        except Exception as e:
            self.lbl_status.setText(f"播放失败: {e}")

    def _on_video_stopped(self):
        """播放窗口停播（关窗/播完/异常）：面板清 embed 播放状态（现状控制行让位）。"""
        self._embed_playing = False
        self.lbl_now.setText("未播放")
        self.slider_pos.setEnabled(False)
        self.slider_pos.setValue(0)
        self.lbl_time.setText("0:00 / 0:00")
        self._sync_toggle_icon()

    def _show_episode_dialog(self, it):
        """多集 → 选集对话框（第01集…，点选即播）。"""
        eps = self._movie.episodes(it)
        if len(eps) <= 1:
            self._play_item(it, index=None)
            return
        dlg = GlassDialog(f"选集 - {(it.get('vod_name') or '')[:24]}", self)
        dlg.resize(320, 420)
        lay = dlg.body_layout
        tip = QLabel(f"共 {len(eps)} 集，双击选集播放（连播开时播完自动下一集）")
        tip.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
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
            # 防重入：字幕搜索线程仍在运行则忽略本次（不覆盖运行中的 QThread）
            if self._sub_thread is not None and self._sub_thread.isRunning():
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

    def _safe_movie_call(self, fn, *args, **kwargs) -> None:
        """后台线程通用包裹：controller 调用放子线程，避免 mpv IPC 超时卡住 UI；
        异常捕获记录，绝不炸线程。"""
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"[Movie] 后台控制调用失败: {e!r}")

    def _cleanup_poster_cache(self, max_files: int = 500):
        """海报磁盘缓存上限：超过 max_files 张按 mtime 删最旧，防磁盘膨胀。"""
        try:
            if not _POSTER_DIR.exists():
                return
            files = [p for p in _POSTER_DIR.iterdir() if p.suffix.lower() == ".jpg"]
            if len(files) <= max_files:
                return
            files.sort(key=lambda p: p.stat().st_mtime)
            drop = files[:-max_files]
            for p in drop:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            logger.info(f"[Movie] 海报缓存清理: 删除 {len(drop)} 张（当前 {len(files)} 张，上限 {max_files}）")
        except Exception as e:
            logger.error(f"[Movie] 海报缓存清理失败: {e!r}")

    def _sync_toggle_icon(self):
        """按真实播放状态刷新 播放/暂停 图标（纯 UI 呈现）。"""
        try:
            playing = bool(self._movie.is_playing())
        except Exception:
            playing = False
        self.btn_toggle.setIcon(lucide_icon(
            "pause" if playing else "play", 20, Colors.TEXT_PRIMARY))

    def _on_toggle(self):
        # 播放中 → 暂停；暂停/停止 → 恢复（toggle 语义）；IPC 下发走后台线程防假死
        threading.Thread(target=self._safe_movie_call, args=(self._movie.toggle_pause,),
                         daemon=True, name="movie-toggle").start()
        # 图标立即翻转（纯 UI 呈现），500ms _tick 轮询兜底校正
        self._sync_toggle_icon()

    # ---- 增强控制 ----

    def _on_fullscreen(self):
        try:
            # embed 内嵌：mpv 无独立窗口（--wid 渲染进容器），set_fullscreen 无效 →
            # 改走播放窗口 Qt 全屏；非 embed 走现状 mpv 全屏。
            if self._embed_playing and self._video_win is not None:
                self._video_win.showFullScreen()
                return
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
        threading.Thread(target=self._safe_movie_call,
                         args=(self._movie.set_volume, value),
                         daemon=True, name="movie-volume").start()

    def _on_mute(self):
        threading.Thread(target=self._safe_movie_call, args=(self._movie.toggle_mute,),
                         daemon=True, name="movie-mute").start()
        self.btn_mute.setIcon(lucide_icon("volume-x", 16, Colors.TEXT_PRIMARY))

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
        glass_info(self, "快捷键说明", table)

    def _on_stop(self):
        threading.Thread(target=self._safe_movie_call, args=(self._movie.stop,),
                         daemon=True, name="movie-stop").start()
        # UI 重置在主线程（controller 最终状态由 _tick 校正）
        self.lbl_now.setText("未播放")
        self.slider_pos.setEnabled(False)
        self.slider_pos.setValue(0)
        self.lbl_time.setText("0:00 / 0:00")

    def _on_seek(self):
        # 只读判定留在主线程（is_playing/get_duration 快速查询），seek 下发走后台线程
        if not self._movie.is_playing():
            return
        dur = self._movie.get_duration()
        if dur > 0:
            ratio = self.slider_pos.value() / 1000.0
            threading.Thread(target=self._safe_movie_call,
                             args=(self._movie.seek, ratio * dur),
                             kwargs={"mode": "absolute"},
                             daemon=True, name="movie-seek").start()

    # ---- 字幕 ----

    def _on_subtitle(self):
        """"字"按钮 → 字幕对话框（搜索框预填当前片名 + 结果列表，双击加载）。"""
        it = self._movie.current_item()
        default_kw = (it or {}).get("vod_name") or ""
        dlg = _SubtitleDialog(self._movie, default_kw, self)
        dlg.exec()

    def _tick(self):
        try:
            # 内嵌播放中：面板轮询让位（连播/进度由播放窗口唯一驱动，防双 tick）
            if self._embed_playing:
                return
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


class _SubtitleDialog(GlassDialog):
    """字幕对话框：搜索框（预填当前片名）+ 结果列表（native_name / 语言 / 格式 / 评分），
    双击条目 → 后台下载加载 → 状态反馈。失败降级不崩。"""

    def __init__(self, movie, default_kw="", parent=None):
        super().__init__("字幕（Assrt）", parent)
        self._movie = movie
        self.resize(460, 420)

        lay = self.body_layout
        row = QHBoxLayout()
        self.edit_kw = QLineEdit(default_kw)
        self.edit_kw.setPlaceholderText("输入片名搜索字幕")
        self.btn_go = QPushButton("搜索")
        self.btn_go.setProperty("class", "primary")
        row.addWidget(self.edit_kw, 1)
        row.addWidget(self.btn_go)
        lay.addLayout(row)

        self.lbl_state = QLabel("")
        self.lbl_state.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        lay.addWidget(self.lbl_state)

        self.list_subs = QListWidget()
        lay.addWidget(self.list_subs, 1)

        self._items = []
        self._thread = None
        self.btn_go.clicked.connect(self._do_search)
        self.edit_kw.returnPressed.connect(self._do_search)
        self.list_subs.itemDoubleClicked.connect(self._on_pick)

    def _do_search(self):
        # 防重入：字幕搜索线程仍在运行则忽略本次
        if self._thread is not None and self._thread.isRunning():
            return
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


class _MovieDetailDialog(GlassDialog):
    """详情页：大图 + 评分/导演/演员/简介 + 播放按钮（对齐爱奇艺点击进详情）。"""

    def __init__(self, it, panel, parent=None):
        super().__init__(f"{(it.get('vod_name') or '')[:24]} - 详情", parent)
        self.resize(540, 660)
        self._panel = panel
        self._it = it

        root = self.body_layout

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
