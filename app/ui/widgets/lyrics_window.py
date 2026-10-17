"""桌面歌词浮窗（基础版）。

- 窗口：Qt.Tool | FramelessWindowHint | WindowStaysOnTopHint（全局置顶浮窗，
  桌面歌词与 video overlay 的 owned-window 语义不同）+ WA_TranslucentBackground
- 渲染：必须 paintEvent 手绘（S7 实锤：QSS background 在 WA_TranslucentBackground
  顶层窗口不渲染 alpha=0）；当前行高亮 + 上下各 1 行渐隐（对齐 Cadence 3 行模式）
- 性能：QFontMetrics 构造一次预热；行文本 → QPixmap 缓存（只重绘变化行）
- 同步：200ms QTimer 查 mpv 播放时间 → 匹配当前行（行跨度 2-4s，200ms 足够）
- 换歌：订阅 MUSIC_TRACK_CHANGED → 重新加载歌词（本地同名 .lrc / 在线 get_lyric）
- 鼠标穿透：WS_EX_TRANSPARENT（S3 经验）；拖拽时临时关穿透，松手恢复
- 降级：无歌词 → "暂无歌词"占位；同步/加载异常不影响播放
"""
import bisect
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ...core.event_bus import EventType
from ...music.lyrics import find_local_lrc, load_lyrics_from_file, parse_lrc

_W = 640          # 窗口宽
_H = 108          # 窗口高（3 行）
_LINE_H = 36      # 行高
_FONT_SIZE = 22   # 歌词字号
_COLOR_CUR = QColor(250, 180, 70)     # 当前行（橙，对齐项目主色）
_COLOR_PREV = QColor(238, 238, 238, 120)  # 上下文行（渐隐白）
_PLACEHOLDER = "♪ 暂无歌词"


class LyricsWindow(QWidget):
    """桌面歌词浮窗：持 controller 引用（播放时间/换歌事件/在线歌词）。"""

    def __init__(self, controller, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._controller = controller
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setFixedSize(_W, _H)

        # 歌词数据：[(time_sec, text)]；当前行索引；标题
        self._lines: List[Tuple[float, str]] = []
        self._title = ""
        self._current_idx = 0
        self._started = False
        # 当前歌标识（轮询比对用：play_online 不发换歌事件，靠 200ms tick 检测）
        self._last_song_key = ""

        # 字体度量预热（构造一次，防每帧 QFontMetrics 卡顿）
        self._font = QFont("Microsoft YaHei", _FONT_SIZE)
        self._fm = QFontMetrics(self._font)

        # 行 pixmap 缓存：text -> (is_current, QPixmap)（只重绘变化行）
        self._row_cache: dict = {}

        # 同步定时器（200ms；行跨度 2-4s，精度足够）
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._sync)

        # 拖拽
        self._dragging = False
        self._drag_offset = None

    # ---- 生命周期 ----
    def start(self) -> None:
        """订阅换歌 + 加载当前歌 + 启动同步（幂等）。"""
        if self._started:
            return
        self._started = True
        try:
            self._controller.event_bus.subscribe(
                EventType.MUSIC_TRACK_CHANGED, self._on_track_changed)
        except Exception:
            pass  # 订阅失败不致命——仍可手动同步
        self._load_current()
        self._timer.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._timer.stop()
        try:
            self._controller.event_bus.unsubscribe(
                EventType.MUSIC_TRACK_CHANGED, self._on_track_changed)
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        self.stop()
        super().closeEvent(event)

    # ---- 歌词加载 ----
    def _load_current(self) -> None:
        try:
            song = self._controller.get_current_song()
        except Exception:
            song = None
        if song is None:
            self.set_lyrics([], "")
            return
        self._load_lyric(song)

    @staticmethod
    def _song_key(song) -> str:
        """歌标识：path|source_name|track_id（dict / OnlineTrack 通吃；None → ""）。"""
        if song is None:
            return ""
        if isinstance(song, dict):
            return "|".join(str(song.get(k) or "") for k in ("path", "source_name", "track_id"))
        return "|".join(str(getattr(song, k, "") or "") for k in ("path", "source_name", "track_id"))

    def _load_lyric(self, song) -> None:
        """本地歌（本地文件路径）：同名 .lrc；在线歌（dict source=online / http path）：manager.get_lyric。失败 → 占位。"""
        # 更新歌标识（事件/轮询两条路都同步，防轮询对同一首歌重复触发）
        self._last_song_key = self._song_key(song)
        # 入口先清空旧歌词——防串词（切歌时旧 lines 残留，加载失败/未完成会继续显示上一首）
        self.set_lyrics([], "")
        title = getattr(song, "title", None) or (song.get("title") if isinstance(song, dict) else None)
        path = song.get("path") if isinstance(song, dict) else None
        # 本地分支：path 必须是本地文件路径（在线歌 dict 也带 path=http URL，误判会丢歌词）
        if path and not str(path).startswith(("http://", "https://")):
            lrc_path = find_local_lrc(path)
            if lrc_path:
                lines = load_lyrics_from_file(lrc_path)
                if lines:
                    self.set_lyrics(lines, title or "")
                    return
            self.set_lyrics([], title or "")
            return
        # 在线歌：OnlineTrack 对象直传；controller 发布的 dict 转 OnlineTrack（补 extra._raw）
        try:
            mgr = getattr(self._controller, "online_manager", None)
            if mgr is None:
                self.set_lyrics([], title or "")
                return
            track = song
            if isinstance(song, dict):
                from ...music.online.models import OnlineTrack
                track = OnlineTrack(
                    source_name=song.get("source_name") or "",
                    track_id=song.get("track_id") or "",
                    title=song.get("title") or "",
                    artist=song.get("artist") or "",
                    album=song.get("album") or "",
                    duration_seconds=song.get("duration") or 0,
                    cover_url=song.get("cover_url"),
                    extra=song.get("extra") or {},
                )
            raw = mgr.get_lyric(track)
            lines = parse_lrc(raw) if raw else []
            self.set_lyrics(lines, title or "")
        except Exception:
            self.set_lyrics([], title or "")

    def set_lyrics(self, lines: List[Tuple[float, str]], title: str = "") -> None:
        self._lines = list(lines)
        self._title = title or ""
        self._current_idx = 0
        self._row_cache.clear()
        self.update()

    def _on_track_changed(self, event) -> None:
        try:
            song = (event.data or {}).get("song")
            if song is None:
                return
            self._load_lyric(song)
        except Exception:
            pass  # 换歌加载失败不崩

    # ---- 同步 ----
    @staticmethod
    def match_index(lines: List[Tuple[float, str]], pos: float) -> int:
        """最后一个 t<=pos 的行索引；pos 早于首行 → 0；空 → -1。"""
        if not lines:
            return -1
        times = [t for t, _ in lines]
        i = bisect.bisect_right(times, pos) - 1
        return max(i, 0)

    def _sync(self) -> None:
        # 轮询检测换歌（play_online 不发 MUSIC_TRACK_CHANGED——防语音链误触）：
        # 现有 200ms tick 顺便比对当前歌标识，变了 → 重新加载（事件订阅保留，双保险）。
        try:
            song = self._controller.get_current_song()
        except Exception:
            song = None
        if song is None:
            if self._last_song_key:
                self._last_song_key = ""
                self.set_lyrics([], "")
                return
            # 无歌且 key 本就为空 → 走时间同步（_lines 可能已有内容）
        key = self._song_key(song)
        if key != self._last_song_key:
            self._load_lyric(song)
            return
        if not self._lines:
            return
        try:
            pos = self._controller.mpv.get_live_playback_time()
        except Exception:
            return
        if pos is None:
            return
        idx = self.match_index(self._lines, float(pos))
        if idx != self._current_idx:
            self._current_idx = idx
            self.update()

    # ---- 渲染（paintEvent 手绘，S7 标准做法）----
    def _row_pixmap(self, text: str, is_current: bool) -> QPixmap:
        """行文本 → 透明底 QPixmap（缓存：同文本+同态复用）。"""
        key = (text, is_current)
        cached = self._row_cache.get(key)
        if cached is not None:
            return cached
        color = _COLOR_CUR if is_current else _COLOR_PREV
        w = self._fm.horizontalAdvance(text) + 8
        h = _LINE_H
        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setFont(self._font)
        painter.setPen(color)
        painter.drawText(4, self._fm.ascent() + 2, text)
        painter.end()
        self._row_cache[key] = pm
        return pm

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if not self._lines:
            # 占位：居中灰字
            pm = self._row_pixmap(_PLACEHOLDER, False)
            x = (self.width() - pm.width()) // 2
            y = (self.height() - pm.height()) // 2
            painter.drawPixmap(x, y, pm)
            return
        idx = self._current_idx
        center_y = self.height() // 2
        # 当前行 + 上下各 1 行（行跨度大时中间可留空隙，这里固定 3 行）
        for rel, line_idx in ((-1, idx - 1), (0, idx), (1, idx + 1)):
            if line_idx < 0 or line_idx >= len(self._lines):
                continue
            text = self._lines[line_idx][1]
            pm = self._row_pixmap(text, rel == 0)
            x = (self.width() - pm.width()) // 2
            y = center_y + rel * _LINE_H - pm.height() // 2
            painter.drawPixmap(x, y, pm)

    # ---- 拖拽（拖时临时关穿透）----
    def _set_passthrough(self, on: bool) -> None:
        try:
            import ctypes
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x20
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_style = style | WS_EX_TRANSPARENT if on else style & ~WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
        except Exception:
            pass  # 非 Windows/无权限 → 不穿透，不影响显示

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._set_passthrough(False)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._drag_offset = None
            self._set_passthrough(True)
