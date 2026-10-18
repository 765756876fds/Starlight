"""MusicPanel 卡片网格 Delegate（Glass 风格自绘卡片）。

性能红线（必须遵守）：
- paint() 内绝不执行文件 IO / 网络请求 / QWidget 实例化 / 正则等慢操作；
- 封面提前异步加载：本地封面走 QThreadPool（后台线程读文件），在线
  cover_url 走 QNetworkAccessManager（主线程异步，非阻塞）；QImage 跨线程
  传递，主线程转 QPixmap 入缓存，paint 仅 drawPixmap；
- hover 微交互：QPropertyAnimation 150ms EASE_OUT（diseno-qt BezierSpline
  0.23,1,0.32,1），仅驱动一个 float 属性插值；reduced-motion 时直接终态；
- 骨架微光：全局 ShimmerClock（1400ms InOutSine 循环）仅在存在「封面加载中」
  的卡片时运行，内容就绪即停——不持续空转；
- 不重写 editorEvent——双击/右键等信号继续由 QListWidget 既有机制发出。
"""
import os

from PySide6.QtCore import (
    QPointF, QPropertyAnimation, QRunnable, QRectF, QSize,
    Qt, QThreadPool, QUrl, QObject, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QAbstractItemView, QStyledItemDelegate, QStyle

from ..theme import motion
from ..theme.tokens import Tokens

# item 附加数据角色（避免与业务 UserRole 冲突）


class MusicCardRoles:
    StatusRole = Qt.UserRole + 40   # str：状态徽标（下载中 xx%），None=无


def cover_key_of(data) -> str:
    """item UserRole dict → 封面缓存 key（仅字符串拼接，无 IO）。
    本地：local:<音频路径>（后台找同目录封面文件）；在线：net:<cover_url>。
    """
    if not isinstance(data, dict):
        return None
    url = data.get("cover_url")
    if url:
        return "net:" + url
    path = data.get("path")
    if path:
        return "local:" + path
    return None


# ---------------------------------------------------------------- 封面加载

class _LocalBridge(QObject):
    """后台线程 → 主线程信号桥（QImage 可跨线程传值）。"""
    local_done = Signal(str, object)


class _LocalCoverTask(QRunnable):
    """后台读同目录封面文件（cover/folder/album/front.*）；不在 UI 线程执行。"""

    _NAMES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png",
              "album.jpg", "album.png", "front.jpg", "front.png")

    def __init__(self, key: str, audio_path: str, bridge: _LocalBridge):
        super().__init__()
        self._key = key
        self._path = audio_path
        self._bridge = bridge

    def run(self):
        img = QImage()
        try:
            d = os.path.dirname(self._path)
            for name in self._NAMES:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    img = QImage(p)
                    if not img.isNull():
                        break
        except Exception:
            img = QImage()
        self._bridge.local_done.emit(self._key, img)


class _CoverLoader(QObject):
    """封面缓存与异步加载（模块级单例，多列表共享）。

    pending 计数驱动全局骨架微光时钟：有封面在加载 → shimmer 运行；
    全部完成/失败 → 时钟停止（motion.ShimmerClock.acquire/release）。
    """

    loaded = Signal(str)   # key：封面已就绪（或失败），可重绘

    def __init__(self):
        super().__init__()
        self._covers = {}            # key -> QPixmap | None（失败）
        self._pending = set()
        self._pending_count = 0
        self._views = {}             # id(view) -> view（微光 tick 重绘范围）
        self._shimmer_connected = False
        self._qnam = QNetworkAccessManager(self)
        self._qnam.finished.connect(self._on_net_finished)
        self._pool = QThreadPool.globalInstance()
        self._bridge = _LocalBridge(self)
        self._bridge.local_done.connect(self._on_local_done)

    # ---- 微光时钟 ----
    def register_view(self, view) -> None:
        """登记使用本 loader 的列表（tick 时只重绘这些 viewport）。"""
        if id(view) in self._views:
            return
        self._views[id(view)] = view
        view.destroyed.connect(
            lambda *_, _id=id(view): self._views.pop(_id, None))

    def _start_shimmer(self) -> None:
        if motion.reduced_motion_enabled():
            return
        clock = motion.ShimmerClock.instance()
        if not self._shimmer_connected:
            clock.tick.connect(self._on_shimmer_tick)
            self._shimmer_connected = True
        clock.acquire()

    def _stop_shimmer(self) -> None:
        motion.ShimmerClock.instance().release()

    def _on_shimmer_tick(self, _pos: float) -> None:
        for view in list(self._views.values()):
            try:
                view.viewport().update()
            except RuntimeError:
                pass  # 视图已销毁，下次 tick 由 destroyed 清理

    def get(self, key: str):
        """返回缓存 QPixmap；None 表示未知（未请求或已失败）。"""
        return self._covers.get(key)

    def failed(self, key: str) -> bool:
        """该 key 已尝试过且失败（画占位即可，勿再请求）。"""
        return key in self._covers and self._covers[key] is None

    def request(self, key: str) -> None:
        """登记异步加载（paint 中调用安全：仅 dict/set 操作 + 入队）。"""
        if key is None or key in self._covers or key in self._pending:
            return
        self._pending.add(key)
        self._pending_count += 1
        if self._pending_count == 1:
            self._start_shimmer()
        if key.startswith("net:"):
            url = key[4:]
            req = QNetworkRequest(QUrl(url))
            req.setTransferTimeout(8000)
            reply = self._qnam.get(req)
            reply.setProperty("cover_key", key)
        else:
            path = key[6:] if key.startswith("local:") else key
            self._pool.start(_LocalCoverTask(key, path, self._bridge))

    def _finish_key(self, key: str) -> None:
        if key in self._pending:
            self._pending.discard(key)
            self._pending_count = max(0, self._pending_count - 1)
            if self._pending_count == 0:
                self._stop_shimmer()

    # ---- 主线程回调 ----
    def _on_local_done(self, key: str, img: QImage):
        self._finish_key(key)
        self._covers[key] = QPixmap.fromImage(img) if not img.isNull() else None
        self.loaded.emit(key)

    def _on_net_finished(self, reply):
        key = reply.property("cover_key")
        reply.deleteLater()
        if not key:
            return
        self._finish_key(key)
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = bytes(reply.readAll())
                img = QImage.fromData(data)
                self._covers[key] = QPixmap.fromImage(img) if not img.isNull() else None
            else:
                self._covers[key] = None
        except Exception:
            self._covers[key] = None
        self.loaded.emit(key)


_loader = None


def _get_cover_loader() -> _CoverLoader:
    global _loader
    if _loader is None:
        _loader = _CoverLoader()
    return _loader


# ---------------------------------------------------------------- Delegate

class MusicCardDelegate(QStyledItemDelegate):
    """160×200 自绘卡片：封面 + 两行文字；hover 150ms 微交互；播放/状态徽标。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loader = _get_cover_loader()
        self._loader.loaded.connect(self._on_cover_loaded)
        if isinstance(self.parent(), QAbstractItemView):
            self._loader.register_view(self.parent())
        self._hover_row = -1
        self.setProperty("hover_t", 0.0)
        self._anim = QPropertyAnimation(self, b"hover_t", self)
        self._anim.setDuration(motion.DUR_HOVER)
        self._anim.setEasingCurve(motion.EASE_OUT)
        self._anim.valueChanged.connect(self._on_anim_step)
        self._anim.finished.connect(self._on_anim_finished)
        self.playing_key = None   # 当前播放卡片 key（由 panel 同步）

    # ---- 尺寸 ----
    def sizeHint(self, option, index) -> QSize:
        return QSize(Tokens.CARD_WIDTH, Tokens.CARD_HEIGHT)

    # ---- 封面就绪 → 重绘 ----
    def _on_cover_loaded(self, key: str):
        v = self.parent()
        if isinstance(v, QAbstractItemView):
            v.viewport().update()

    # ---- hover 动画 ----
    def _on_anim_step(self, _v):
        v = self.parent()
        if isinstance(v, QAbstractItemView):
            v.viewport().update()

    def _on_anim_finished(self):
        if self._anim.direction() == QPropertyAnimation.Backward:
            self._hover_row = -1
            self.setProperty("hover_t", 0.0)
            v = self.parent()
            if isinstance(v, QAbstractItemView):
                v.viewport().update()

    # ---- 绘制 ----
    def paint(self, painter: QPainter, option, index) -> None:
        data = index.data(Qt.UserRole)
        if not isinstance(data, dict):
            data = {}
        over = bool(option.state & QStyle.State_MouseOver)
        cur = index.row()
        reduced = motion.reduced_motion_enabled()
        # hover 状态机：进入 → 正向动画；离开 → 反向动画（收尾渐变）
        if not reduced and over and cur != self._hover_row:
            self._hover_row = cur
            self.setProperty("hover_t", 0.0)
            self._anim.setDirection(QPropertyAnimation.Forward)
            self._anim.start()
        elif not reduced and not over and cur == self._hover_row:
            self.setProperty("hover_t", 1.0)
            self._anim.setDirection(QPropertyAnimation.Backward)
            self._anim.start()
        if reduced:
            t = 1.0 if over else 0.0          # 减少动画：直接终态
        else:
            t = float(self.property("hover_t") or 0.0) if cur == self._hover_row else 0.0

        selected = bool(option.state & QStyle.State_Selected)
        playing = (self.playing_key is not None
                   and data.get("_play_key") == self.playing_key)
        status = index.data(MusicCardRoles.StatusRole)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, Tokens.RADIUS, Tokens.RADIUS)
        painter.setClipPath(path)

        # 背景：顶部受光 → 底部渐沉的玻璃渐变（alpha 随 hover 呼吸）
        alpha = int(255 * _lerp_f(Tokens.CARD_ALPHA, Tokens.CARD_HOVER_ALPHA, t))
        top_c = _lerp(QColor(*_split(Tokens.CARD_TOP_RGB), alpha),
                      QColor(*_split(Tokens.CARD_HOVER_TOP_RGB), alpha), t)
        bot_c = _lerp(QColor(*_split(Tokens.CARD_BOTTOM_RGB), alpha),
                      QColor(*_split(Tokens.CARD_HOVER_BOTTOM_RGB), alpha), t)
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, top_c)
        grad.setColorAt(1.0, bot_c)
        if selected:
            acc = QColor(*_split(Tokens.ACCENT_RGB), 70)
            grad.setColorAt(0.0, _lerp(top_c, acc, 0.45))
            grad.setColorAt(1.0, _lerp(bot_c, acc, 0.30))
        painter.fillPath(path, grad)

        # 玻璃受光高光：顶部 1px 细亮线（hover 更亮） / 底部 1px 暗线
        painter.setPen(QPen(QColor(255, 255, 255,
                                   int(_lerp_f(_TOP_LINE_A, _TOP_LINE_HOVER_A, t)))))
        _glass_line(painter, rect, top=True)
        painter.setPen(QPen(QColor(0, 0, 0, _BOTTOM_LINE_A)))
        _glass_line(painter, rect, top=False)

        # 边框：默认弱灰 → hover 细高光（alpha/色相插值）
        b_default = QColor(*[int(c) for c in _split(Tokens.BORDER_RGB)], 90)
        b_hover = QColor(*[int(c) for c in _split(Tokens.ACCENT_RGB)], 115)
        if playing:
            pen = QPen(QColor(Tokens.ACCENT), 2)
        elif selected:
            pen = QPen(QColor(*[int(c) for c in _split(Tokens.ACCENT_RGB)], 150), 1)
        else:
            pen = QPen(_lerp(b_default, b_hover, t), 1)
        painter.setPen(pen)
        painter.drawPath(path)

        pad = Tokens.CARD_PADDING
        cover = QRectF(rect.left() + pad, rect.top() + pad,
                       rect.width() - 2 * pad, rect.width() - 2 * pad)
        key = cover_key_of(data)
        pm = self._loader.get(key) if key else None
        if pm is not None:
            painter.drawPixmap(cover.toRect(), pm)
        else:
            self._paint_skeleton(painter, cover)
            if key and not self._loader.failed(key):
                self._loader.request(key)   # 仅登记异步加载，无 IO

        if playing:
            self._paint_playing_badge(painter, cover)
        if status:
            self._paint_status_badge(painter, cover, status)

        # 文字两行：歌名 14 加粗主色 / 辅助 12 次色，elidedText 截断
        text_top = cover.bottom() + 6
        text_w = rect.width() - 2 * pad
        f1 = QFont()
        f1.setPixelSize(Tokens.FONT_SIZE_BODY)
        f1.setBold(True)
        fm1 = QFontMetrics(f1)
        title = data.get("title") or "?"
        title = fm1.elidedText(str(title), Qt.ElideRight, int(text_w))
        painter.setFont(f1)
        painter.setPen(QColor(Tokens.TEXT_PRIMARY))
        painter.drawText(QPointF(rect.left() + pad, text_top + fm1.ascent()), title)

        f2 = QFont()
        f2.setPixelSize(Tokens.FONT_SIZE_SUB)
        fm2 = QFontMetrics(f2)
        sub = data.get("_sub") or data.get("artist") or ""
        sub = fm2.elidedText(str(sub), Qt.ElideRight, int(text_w))
        painter.setFont(f2)
        painter.setPen(QColor(Tokens.TEXT_SECONDARY))
        painter.drawText(
            QPointF(rect.left() + pad, text_top + fm1.height() + 2 + fm2.ascent()), sub)
        painter.restore()

    # ---- 骨架占位（封面未就绪：渐变 + 微光扫描 + 音符） ----
    @staticmethod
    def _paint_skeleton(painter: QPainter, cover: QRectF):
        grad = QLinearGradient(cover.topLeft(), cover.bottomRight())
        grad.setColorAt(0.0, QColor(52, 52, 80))
        grad.setColorAt(1.0, QColor(34, 34, 52))
        painter.fillRect(cover, grad)
        # 微光扫描线（ui-ux-pro-max motion：#15 gradient sweep, sine.inOut, 1400ms 循环）；
        # 时钟仅在存在加载中封面时运行（_CoverLoader pending 计数），reduced-motion 静态。
        clock = motion.ShimmerClock.instance()
        if clock.active():
            pos = clock.pos()
            span = cover.width() + 120.0
            sx = cover.left() - 60.0 + span * pos
            sweep = QLinearGradient(QPointF(sx - 45, cover.top()),
                                    QPointF(sx + 45, cover.top()))
            sweep.setColorAt(0.0, QColor(255, 255, 255, 0))
            sweep.setColorAt(0.5, QColor(255, 255, 255, _SHIMMER_A))
            sweep.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillRect(cover, sweep)
        f = QFont()
        f.setPixelSize(52)
        painter.setFont(f)
        painter.setPen(QColor(150, 150, 185, 110))
        painter.drawText(cover, Qt.AlignCenter, "♪")

    # ---- 播放徽标（封面左上：accent 圆 + 白三角） ----
    @staticmethod
    def _paint_playing_badge(painter: QPainter, cover: QRectF):
        c = cover.topLeft() + QPointF(6, 6)
        r = 12.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(Tokens.ACCENT))
        painter.drawEllipse(QPointF(c.x() + r, c.y() + r), r, r)
        painter.setBrush(QColor("#ffffff"))
        tri = QPolygonF([
            QPointF(c.x() + r - 3.5, c.y() + r - 5.5),
            QPointF(c.x() + r - 3.5, c.y() + r + 5.5),
            QPointF(c.x() + r + 5.5, c.y() + r),
        ])
        painter.drawPolygon(tri)

    # ---- 状态徽标（封面右下：下载进度等） ----
    @staticmethod
    def _paint_status_badge(painter: QPainter, cover: QRectF, text: str):
        f = QFont()
        f.setPixelSize(Tokens.FONT_SIZE_SUB)
        painter.setFont(f)
        fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 12
        h = 20.0
        x = cover.right() - w - 6
        y = cover.bottom() - h - 6
        badge = QRectF(x, y, w, h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 20, 30, 190))
        painter.drawRoundedRect(badge, 6, 6)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(badge, Qt.AlignCenter, text)


def _split(rgb: str) -> list:
    """'r, g, b' → [r, g, b]。"""
    return [int(v.strip()) for v in rgb.split(",")]


def _lerp(c1: QColor, c2: QColor, t: float) -> QColor:
    t = min(max(t, 0.0), 1.0)
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue() + (c2.blue() - c1.blue()) * t),
        int(c1.alpha() + (c2.alpha() - c1.alpha()) * t),
    )


def _lerp_f(a: float, b: float, t: float) -> float:
    return a + (b - a) * min(max(t, 0.0), 1.0)


def _rgba_alpha(rgba_str: str) -> int:
    """'rgba(255, 255, 255, 26)' → 26（模块级预解析一次，paint 不重复解析）。"""
    inner = rgba_str[rgba_str.index("(") + 1: rgba_str.index(")")]
    return int(inner.split(",")[-1].strip())


def _glass_line(painter: QPainter, rect: QRectF, top: bool) -> None:
    """沿圆角卡片上/下缘的 1px 高光线（clip 已限圆角，直接画直线）。"""
    r = float(Tokens.RADIUS)
    y = rect.top() + 0.5 if top else rect.bottom() - 0.5
    painter.drawLine(QPointF(rect.left() + r, y),
                     QPointF(rect.right() - r, y))


# 玻璃高光线 alpha（tokens 预解析；模块级常量，paint 零解析成本）
_TOP_LINE_A = _rgba_alpha(Tokens.GLASS_TOP_LINE)
_TOP_LINE_HOVER_A = _rgba_alpha(Tokens.GLASS_TOP_LINE_HOVER)
_BOTTOM_LINE_A = _rgba_alpha(Tokens.GLASS_BOTTOM_LINE)
_SHIMMER_STR = getattr(Tokens, "SHIMMER_LINE", "rgba(255, 255, 255, 30)")
_SHIMMER_A = _rgba_alpha(_SHIMMER_STR) if "rgba(" in _SHIMMER_STR else 30
