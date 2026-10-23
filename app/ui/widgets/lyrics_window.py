"""桌面歌词浮窗（基础版 + 批 3b 自定义）。

- 窗口：Qt.Tool | FramelessWindowHint | WindowStaysOnTopHint（全局置顶浮窗，
  桌面歌词与 video overlay 的 owned-window 语义不同）+ WA_TranslucentBackground
- 渲染：必须 paintEvent 手绘（S7 实锤：QSS background 在 WA_TranslucentBackground
  顶层窗口不渲染 alpha=0）；当前行高亮 + 上下各 1 行渐隐（对齐 Cadence 3 行模式）
- 性能：QFontMetrics 构造一次预热；行文本 → QPixmap 缓存（只重绘变化行）
- 同步：200ms QTimer 查 mpv 播放时间 → 匹配当前行（行跨度 2-4s，200ms 足够）
- 换歌：订阅 MUSIC_TRACK_CHANGED → 重新加载歌词（本地同名 .lrc / 在线 get_lyric）；
  play_online 不发事件 → 200ms tick 轮询比对歌标识自动重载（事件订阅双保险）
- 鼠标穿透：WS_EX_TRANSPARENT（S3 经验）；拖拽时临时关穿透，松手恢复
- 批 3b 自定义：字体/字号/颜色/背景透明度/单双行/位置记忆（settings.yaml music.lyrics 子段，
  设置页改动即时生效 apply_prefs；拖拽松手保存坐标，越界回中央）
- 降级：无歌词 → "暂无歌词"占位；同步/加载异常不影响播放；配置读取失败 → 默认值
"""
import bisect
import threading

from PySide6.QtCore import QRect, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

from ...core.event_bus import EventType
from ...music.lyrics import find_local_lrc, load_lyrics_from_file, parse_lrc
from ..settings_store import load_lyrics_prefs, save_lyrics_prefs

_W = 640          # 窗口宽
_FONT_SIZE = 24   # 默认字号
_COLOR_CUR_HEX = "#FAB446"  # 默认当前行颜色（橙）
_PLACEHOLDER = "♪ 暂无歌词"


class LyricsWindow(QWidget):
    """桌面歌词浮窗：持 controller 引用（播放时间/换歌事件/在线歌词）。"""

    # ---- 线程安全歌词加载信号（事件可能来自轮询/Timer 线程） ----
    # 槽必须是本 QWidget 的方法（QObject receiver）：从其他线程 emit → 自动
    # QueuedConnection（投递到窗口线程事件循环，Qt 控件操作绝不跨线程直执行）；
    # 从窗口线程 emit → DirectConnection（同步执行）。禁止把槽换成纯函数/闭包
    # ——PySide6 对非 QObject receiver 退化为 Direct，跨线程会直执行 Qt 操作。
    _track_requested = Signal(object)               # (snapshot) → 主线程登记请求+加载
    _lyric_fetched = Signal(object, object, object)  # (seq, snapshot, lines) → 主线程应用

    def __init__(self, controller, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._controller = controller
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NativeWindow)

        # 歌词数据：[(time_sec, text)]；当前行索引；标题
        self._lines: list[tuple[float, str]] = []
        self._title = ""
        self._current_idx = 0
        self._started = False
        # 当前歌标识（轮询比对用：play_online 不发换歌事件，靠 200ms tick 检测）
        self._last_song_key = ""
        # 歌词请求代次（在线取词过期校验：旧请求迟到结果不得覆盖新歌）与关闭守卫
        self._lyric_seq = 0
        self._closed = False
        self._track_requested.connect(self._on_lyric_requested)
        self._lyric_fetched.connect(self._on_lyric_fetched)

        # 批 3b 自定义配置（settings.yaml music.lyrics；损坏 → 默认值兜底）
        self._prefs = dict(load_lyrics_prefs())
        self._color_cur = QColor(str(self._prefs.get("color") or _COLOR_CUR_HEX))
        self._color_prev = QColor(self._color_cur.red(), self._color_cur.green(),
                                  self._color_cur.blue(), 100)
        # 字体度量先就绪（行高取实际 ascent+descent+leading，防大字号文字贴顶/贴底被切）
        self._apply_font()
        self._line_h = self._line_height()
        self._apply_window_size()
        self._apply_position(self._prefs)

        # 行 pixmap 缓存：text -> (is_current, QPixmap)（只重绘变化行）
        self._row_cache: dict = {}

        # 同步定时器（200ms；行跨度 2-4s，精度足够）
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._sync)

        # 拖拽（locked=True=锁定/穿透，不可拖；False=解锁/可拖）
        self._dragging = False
        self._drag_offset = None
        self._apply_passthrough()

    # ---- 批 3b 自定义（字体/字号/颜色/透明度/模式/位置/锁定） ----
    def apply_prefs(self, prefs: dict) -> None:
        """设置页即时生效入口：合并更新配置，重建字体度量/清行缓存/穿透状态。

        幂等：无变化直接返回；字体/字号变 → 重建 QFontMetrics（换字体才清缓存重算）；
        颜色/模式变 → 清行缓存 + 重设窗口高；locked 变 → 立即切换鼠标穿透。
        **位置处理仅当 prefs 显式含 pos_x/pos_y 才执行**（设置页改动不含 pos →
        绝不挪动窗口；只有拖拽保存/重置按钮才带位置意图）。
        """
        if not isinstance(prefs, dict):
            return
        merged = dict(self._prefs)
        merged.update({k: v for k, v in prefs.items() if k in merged})
        if merged == self._prefs:
            return
        locked_changed = merged.get("locked") != self._prefs.get("locked")
        self._prefs = merged
        self._color_cur = QColor(str(merged.get("color") or _COLOR_CUR_HEX))
        self._color_prev = QColor(self._color_cur.red(), self._color_cur.green(),
                                  self._color_cur.blue(), 100)
        self._apply_font()          # 字体/字号 → 重建 QFontMetrics
        self._line_h = self._line_height()
        self._row_cache.clear()     # 颜色/字号/字体变 → 缓存失效（换字体才清，此处统一清一次）
        self._apply_window_size()
        # 仅显式位置意图才处理位置（改字体/锁定/颜色等绝不挪动/绝不 recenter）
        if "pos_x" in prefs or "pos_y" in prefs:
            self._apply_position(merged)
        if locked_changed:
            self._apply_passthrough()
        self.update()

    def _line_height(self) -> int:
        """实际字体行高（QFontMetrics.height()=ascent+descent+leading）+ 上下边距——
        保证文字完整落在行 pixmap 内，不贴顶/贴底被切（估算 size*1.5 会低估大字号）。"""
        return self._fm.height() + 8

    def _line_count(self) -> int:
        """按模式：triple=3 行上下文；single=单行。"""
        return 3 if str(self._prefs.get("mode") or "triple") == "triple" else 1

    def _apply_font(self) -> None:
        self._font = QFont(str(self._prefs.get("font") or "Microsoft YaHei"),
                           int(self._prefs.get("font_size") or _FONT_SIZE))
        self._fm = QFontMetrics(self._font)

    def _apply_window_size(self) -> None:
        # 窗口高 = 行数 × 实际行高 + 上下边距（首行不贴顶、末行不贴底）
        self.setFixedSize(_W, self._line_count() * self._line_h + 12)

    def _apply_position(self, prefs: dict) -> None:
        x, y = prefs.get("pos_x"), prefs.get("pos_y")
        if x is None or y is None:
            self._recenter()
            return
        if self._pos_on_screen(int(x), int(y), self.width(), self.height()):
            self.move(int(x), int(y))
        else:
            self._recenter()

    def _recenter(self) -> None:
        """回主屏/鼠标所在屏中央（可用区域）。"""
        try:
            sc = QGuiApplication.primaryScreen()
            if sc is None:
                return
            geo = sc.availableGeometry()
            self.move(geo.center().x() - self.width() // 2,
                      geo.center().y() - self.height() // 2)
        except Exception:
            pass  # 无屏/异常 → 不动，默认位置

    @staticmethod
    def _pos_on_screen(x: int, y: int, w: int, h: int) -> bool:
        """越界保护：坐标矩形是否与任一屏幕可用区域相交（多屏任一屏即可）。"""
        try:
            rect = QRect(int(x), int(y), int(w), int(h))
            for sc in QGuiApplication.screens():
                if sc.availableGeometry().intersects(rect):
                    return True
            return False
        except Exception:
            return False

    # ---- 生命周期 ----
    def start(self) -> None:
        """订阅换歌 + 加载当前歌 + 启动同步（幂等）。

        关闭（closeEvent 置 _closed=True）后同一实例重开也走本入口：
        复位关闭守卫，恢复本地/在线歌词加载；关闭期间（重开前）迟到
        结果仍被 _closed 丢弃，保护不被破坏。
        """
        if self._started:
            return
        self._closed = False
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
        # 关闭守卫：后台取词线程完成时据此丢弃迟到结果（不 emit 到已销毁的 QObject）。
        # 置位在 stop() 之外——stop 可单独调用（窗口仍存活），仅关闭才禁后续加载。
        self._closed = True
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
        self._request_lyric(song)

    @staticmethod
    def _song_key(song) -> str:
        """歌标识：path|source_name|track_id（dict / OnlineTrack 通吃；None → ""）。"""
        if song is None:
            return ""
        if isinstance(song, dict):
            return "|".join(str(song.get(k) or "") for k in ("path", "source_name", "track_id"))
        return "|".join(str(getattr(song, k, "") or "") for k in ("path", "source_name", "track_id"))

    @staticmethod
    def _song_snapshot(song) -> dict | None:
        """稳定快照：只取加载所需字段，不持有 event/controller 原对象。
        后台取词期间原对象可能被后续请求复用，快照保证迟到结果仍反映本请求。"""
        if song is None:
            return None
        key = LyricsWindow._song_key(song)
        if not key:
            return None
        if isinstance(song, dict):
            extra = song.get("extra") or {}
            snapshot = {
                "key": key,
                "title": str(song.get("title") or ""),
                "artist": str(song.get("artist") or ""),
                "path": str(song.get("path") or ""),
                "source_name": str(song.get("source_name") or ""),
                "track_id": str(song.get("track_id") or ""),
            }
        else:
            extra = getattr(song, "extra", None) or {}
            snapshot = {
                "key": key,
                "title": str(getattr(song, "title", "") or ""),
                "artist": str(getattr(song, "artist", "") or ""),
                "path": str(getattr(song, "path", "") or ""),
                "source_name": str(getattr(song, "source_name", "") or ""),
                "track_id": str(getattr(song, "track_id", "") or ""),
            }
        # extra 浅拷贝（dict 层级；取词服务可能依赖其中的源私有字段）
        snapshot["extra"] = dict(extra)
        return snapshot

    def _request_lyric(self, song) -> None:
        """任意线程入口：生成稳定快照并投递到窗口线程。

        事件路径（轮询/Timer 线程，可能在音乐业务锁内）只做 O(1) 的
        snapshot + emit——网络/文件读取绝不在本调用内执行，不阻塞发布方；
        Qt 控件操作经信号在窗口线程执行。窗口线程自身调用（_load_current /
        _sync 轮询）→ Direct 同步，与旧行为一致。
        """
        snapshot = self._song_snapshot(song)
        if snapshot is None:
            return
        self._track_requested.emit(snapshot)

    @Slot(object)
    def _on_lyric_requested(self, snapshot: dict) -> None:
        """窗口线程：登记最新请求代次 + 清空旧歌词（防串词）。
        本地歌：主线程直接读 .lrc（文件读 ms 级）；在线歌：后台线程取词。"""
        if self._closed:
            return
        self._lyric_seq += 1
        seq = self._lyric_seq
        self._last_song_key = snapshot["key"]
        # 入口先清空旧歌词——切歌时旧 lines 残留会继续显示上一首（保留原语义）
        self.set_lyrics([], "")
        title = snapshot["title"]
        path = snapshot["path"]
        # 本地分支：path 必须是本地文件路径（在线歌 dict 也带 path=http URL，误判会丢歌词）
        if path and not path.startswith(("http://", "https://")):
            self._apply_lyric(snapshot, self._load_local_lyric(path))
            return
        # 在线歌：网络获取放后台线程（绝不阻塞窗口线程/音乐发布方），
        # 结果带请求代次回窗口线程，由 _on_lyric_fetched 做过期校验。
        threading.Thread(
            target=self._fetch_lyric_worker,
            args=(snapshot, seq),
            daemon=True,
            name="lyrics-fetch",
        ).start()

    def _load_local_lyric(self, path: str) -> list[tuple[float, str]]:
        """本地同名 .lrc 读取（仅窗口线程调用；失败 → 空列表 → 占位）。"""
        try:
            lrc_path = find_local_lrc(path)
            if lrc_path:
                lines = load_lyrics_from_file(lrc_path)
                if lines:
                    return lines
        except Exception:
            pass
        return []

    def _fetch_lyric_worker(self, snapshot: dict, seq: int) -> None:
        """后台线程：在线歌词获取（网络）+ 解析。不触碰任何 Qt 对象。
        网络异常 → 空歌词（占位），本请求线程结束，不影响后续请求。"""
        try:
            raw = self._fetch_online_lyric(snapshot)
            lines = parse_lrc(raw) if raw else []
        except Exception:
            lines = []
        # 窗口已关闭 → 丢弃迟到结果，绝不 emit 到已销毁的 QObject
        if self._closed:
            return
        self._lyric_fetched.emit(seq, snapshot, lines)

    def _fetch_online_lyric(self, snapshot: dict):
        """在线歌词获取（后台线程调用；全异常兜底返回 None）。"""
        try:
            mgr = getattr(self._controller, "online_manager", None)
            if mgr is None:
                return None
            from ...music.online.models import OnlineTrack
            track = OnlineTrack(
                source_name=snapshot["source_name"],
                track_id=snapshot["track_id"],
                title=snapshot["title"],
                artist=snapshot["artist"],
                album="",
                duration_seconds=0,
                cover_url=None,
                extra=snapshot["extra"],
            )
            return mgr.get_lyric(track)
        except Exception:
            return None

    @Slot(object, object, object)
    def _on_lyric_fetched(self, seq: int, snapshot: dict, lines: list) -> None:
        """窗口线程：应用在线取词结果。过期校验——期间已有新请求（代次递增）
        → 丢弃旧结果，绝不覆盖新歌歌词。"""
        if self._closed:
            return
        if seq != self._lyric_seq:
            return  # 旧请求迟到结果：不覆盖新歌歌词
        self._apply_lyric(snapshot, lines)

    def _apply_lyric(self, snapshot: dict, lines: list) -> None:
        """窗口线程：写入歌词并重绘（无歌词 → 占位标题，保留原语义）。"""
        if self._closed:
            return
        title = snapshot["title"]
        if lines:
            self.set_lyrics(lines, title)
        else:
            self.set_lyrics([], title)

    def set_lyrics(self, lines: list[tuple[float, str]], title: str = "") -> None:
        self._lines = list(lines)
        self._title = title or ""
        self._current_idx = 0
        self._row_cache.clear()
        self.update()

    def _on_track_changed(self, event) -> None:
        try:
            if self._closed:
                return
            song = (event.data or {}).get("song")
            if song is None:
                return
            self._request_lyric(song)
        except Exception:
            pass  # 换歌加载失败不崩

    # ---- 同步 ----
    @staticmethod
    def match_index(lines: list[tuple[float, str]], pos: float) -> int:
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
            self._request_lyric(song)
            return
        if not self._lines:
            return
        try:
            # 读内存快照（controller 后台轮询线程更新），禁调 mpv IPC——200ms 高频
            # 刷新绝不能阻塞 GUI 线程。
            pos = self._controller.current_playback_time
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
        """行文本 → 透明底 QPixmap（缓存：同文本+同态复用；换字体/字号/颜色清缓存）。"""
        key = (text, is_current)
        cached = self._row_cache.get(key)
        if cached is not None:
            return cached
        color = self._color_cur if is_current else self._color_prev
        w = self._fm.horizontalAdvance(text) + 8
        h = self._line_h
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
        # 背景透明度（0=纯透明不画；>0 画半透黑底，alpha 上限 180 保证文字清晰）
        op = int(self._prefs.get("bg_opacity") or 0)
        if op > 0:
            painter.fillRect(self.rect(), QColor(0, 0, 0, int(op / 100 * 180)))
        if not self._lines:
            # 占位：居中灰字
            pm = self._row_pixmap(_PLACEHOLDER, False)
            x = (self.width() - pm.width()) // 2
            y = (self.height() - pm.height()) // 2
            painter.drawPixmap(x, y, pm)
            return
        idx = self._current_idx
        center_y = self.height() // 2
        # 当前行 + 上下文行（triple=上下各 1；single=仅当前行）
        rels = (-1, 0, 1) if self._line_count() == 3 else (0,)
        for rel, line_idx in ((rel, idx + rel) for rel in rels):
            if line_idx < 0 or line_idx >= len(self._lines):
                continue
            text = self._lines[line_idx][1]
            pm = self._row_pixmap(text, rel == 0)
            x = (self.width() - pm.width()) // 2
            y = center_y + rel * self._line_h - pm.height() // 2
            painter.drawPixmap(x, y, pm)

    # ---- 拖拽/穿透（locked=True=锁定/穿透不可拖；False=解锁/可拖）----
    def _apply_passthrough(self) -> None:
        """按锁定态应用鼠标穿透（构造/设置页切换/show 时调用；winId 未就绪 → 下次再补）。"""
        self._set_passthrough(bool(self._prefs.get("locked", True)))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # winId 在 show 后才有有效句柄——构造时 _apply_passthrough 可能静默失败，show 时补一次
        self._apply_passthrough()

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
            pass  # 非 Windows/无权限/winId 未就绪 → 不穿透，不影响显示

    def mousePressEvent(self, event) -> None:
        # 仅解锁态可拖拽（锁定时 WS_EX_TRANSPARENT 穿透，鼠标事件到不了窗口；guard 双保险）
        if event.button() == Qt.LeftButton and not self._dragging \
                and not self._prefs.get("locked", True):
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
            # 松手保持解锁态（用户可继续拖，直到设置页勾回"锁定"）；解锁态无需恢复穿透
            # 位置保存（拖拽结束 → 实时保存坐标；越界由 _apply_position 兜底）
            try:
                pos = self.pos()
                save_lyrics_prefs({"pos_x": pos.x(), "pos_y": pos.y()})
            except Exception as e:
                # 位置记忆失败不能静默——否则 yaml pos 保持 None，下次 apply_prefs 会 recenter
                print(f"[LyricsWindow] 拖拽位置保存失败: {e!r}")
