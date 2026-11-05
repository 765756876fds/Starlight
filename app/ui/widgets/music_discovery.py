"""Music discovery workbench backed exclusively by existing controller APIs."""
import time
import json
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QButtonGroup,
    QComboBox, QScrollArea, QStackedWidget, QLineEdit)

from ...core.notifications import notify
from .latest_task import LatestTask
from .discovery_cards import CardList, ContentSection, label


def music_card(row):
    return dict(title=row.get("title") or row.get("name") or "未命名",
                subtitle=row.get("artist") or row.get("description") or "",
                caption=row.get("source_name") or ("本地" if row.get("source_type") == "local" else ""),
                cover_url=row.get("cover_url") or row.get("artwork") or row.get("coverImg") or "")


class MusicDiscovery(QWidget):
    tracks_ready = Signal(object, str)
    navigate = Signal(int)
    history_chosen = Signal(object)
    playlist_chosen = Signal(object)
    paging_changed = Signal(bool)

    def __init__(self, music, parent=None):
        super().__init__(parent)
        self.music = music
        self._view = "home"
        self._page = 1
        self._last_request = None
        self._sheet = None
        self._tracks = []
        self._track_pages = {}
        self._capabilities = {}
        self._catalog = None
        self._catalog_error = None
        self._catalog_at = 0
        self._browse_cache = {}
        self._history, self._playlists = [], []
        self._started = False
        self._task = LatestTask(self)
        self._catalog_task = LatestTask(self)
        self._local_task = LatestTask(self)
        self._task.completed.connect(self._loaded)
        self._catalog_task.completed.connect(self._catalog_loaded)
        self._local_task.completed.connect(self._local_loaded)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.navigation = QButtonGroup(self)
        self.nav_buttons = {}
        for key, title in (("home", "推荐"), ("ranks", "排行榜"), ("sheets", "歌单"),
                           ("new", "新歌"), ("artists", "歌手 / 专辑"), ("history", "最近播放")):
            button = QPushButton(title)
            button.setCheckable(True)
            self.navigation.addButton(button)
            self.nav_buttons[key] = button
            button.clicked.connect(lambda _=False, view=key: self.show_view(view))
            nav.addWidget(button)
        self.nav_buttons["home"].setChecked(True)
        nav.addStretch()
        root.addLayout(nav)
        controls = QHBoxLayout()
        controls.addWidget(label("发现音源", True))
        self.sources = QComboBox()
        self.sources.setMinimumContentsLength(8)
        controls.addWidget(self.sources, 1)
        self.tags = QComboBox()
        self.tags.setMinimumContentsLength(6)
        controls.addWidget(self.tags, 1)
        self.refresh_button = QPushButton("刷新内容")
        self.refresh_button.clicked.connect(self.retry)
        self.back = QPushButton("发现首页")
        self.back.clicked.connect(lambda: self.show_view("home"))
        self.back.hide()
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.back)
        root.addLayout(controls)
        self.status = label("内容由音源实际能力提供；歌曲版本与来源均保留。", True)
        root.addWidget(self.status)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.home = QScrollArea()
        self.home.setWidgetResizable(True)
        self.home.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.home.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 6, 8)
        layout.setSpacing(18)
        self.sections = {}
        for key, title in (("today", "今日推荐"), ("ranks", "热门排行榜"), ("featured", "精选歌单"),
                           ("new", "新歌推荐"), ("recent", "最近播放"), ("mine", "我的歌单")):
            section = ContentSection(title, self)
            self.sections[key] = section
            layout.addWidget(section)
        for key in ("today", "featured"):
            self.sections[key].all_button.clicked.connect(lambda: self.show_view("sheets"))
            self.sections[key].cards.itemDoubleClicked.connect(self._open_sheet)
        for key in ("ranks", "new"):
            self.sections[key].all_button.clicked.connect(lambda _=False, k=key: self.show_view(k))
            self.sections[key].cards.itemDoubleClicked.connect(self._open_chart)
        self.sections["recent"].all_button.clicked.connect(lambda: self.navigate.emit(2))
        self.sections["recent"].cards.itemDoubleClicked.connect(lambda item: self.history_chosen.emit(item.data(Qt.UserRole)))
        self.sections["mine"].all_button.clicked.connect(lambda: self.navigate.emit(3))
        self.sections["mine"].cards.itemDoubleClicked.connect(lambda item: self.playlist_chosen.emit(item.data(Qt.UserRole)))
        layout.addStretch()
        self.pages.addWidget(self.home)
        self.category_page = QWidget()
        category_layout = QVBoxLayout(self.category_page)
        category_layout.setContentsMargins(0, 0, 0, 0)
        self.category_title = label()
        self.category_title.setProperty("class", "title")
        category_layout.addWidget(self.category_title)
        artist_row = QHBoxLayout()
        self.artist_kind = QComboBox()
        self.artist_kind.addItem("歌手", "artist")
        self.artist_kind.addItem("专辑", "album")
        self.artist_query = QLineEdit()
        self.artist_query.setPlaceholderText("输入歌手或专辑名称")
        self.artist_search = QPushButton("查找")
        artist_row.addWidget(self.artist_kind)
        artist_row.addWidget(self.artist_query, 1)
        artist_row.addWidget(self.artist_search)
        self.artist_controls = QWidget()
        self.artist_controls.setLayout(artist_row)
        category_layout.addWidget(self.artist_controls)
        self.artist_search.clicked.connect(self._find_artist)
        self.artist_query.returnPressed.connect(self._find_artist)
        self.artist_kind.currentIndexChanged.connect(self._artist_kind_changed)
        self.sheets = CardList(self)
        self.sheets.itemDoubleClicked.connect(self._open_category_item)
        category_layout.addWidget(self.sheets, 1)
        self.empty = label("", True)
        category_layout.addWidget(self.empty)
        self.more = QPushButton("加载更多")
        self.more.clicked.connect(self._more)
        self.more.hide()
        category_layout.addWidget(self.more)
        self.pages.addWidget(self.category_page)
        self.sources.currentIndexChanged.connect(self._source_changed)
        self.tags.currentIndexChanged.connect(self._load_sheets)
        self.refresh_sources()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._started:
            self._started = True
            self._source_changed()
            self._refresh_local()

    def shutdown(self):
        for task in (self._task, self._catalog_task, self._local_task):
            task.cancel()

    def refresh_sources(self):
        self._task.invalidate()
        self._catalog_task.invalidate()
        self._catalog = None
        self._catalog_error = None
        self._browse_cache.clear()
        previous = self.sources.currentData()
        getter = getattr(self.music, "discovery_sources", None)
        try:
            sources = getter() if callable(getter) else []
            sources = sources if isinstance(sources, (list, tuple)) else []
        except Exception:
            sources = []
        self._capabilities = {s["name"]: s.get("capabilities", {}) for s in sources
                              if isinstance(s, dict) and s.get("name") and any(s.get("capabilities", {}).values())}
        self.sources.blockSignals(True)
        self.sources.clear()
        for name in self._capabilities:
            self.sources.addItem(name, name)
        if self.sources.findData(previous) >= 0:
            self.sources.setCurrentIndex(self.sources.findData(previous))
        self.sources.blockSignals(False)
        self.refresh_button.setEnabled(bool(self.sources.count()))
        self._last_request = None
        self._sheet = None
        self._render_charts()
        self._render_sheets([])
        self.status.setText("选择发现音源，或直接使用顶部搜索。" if self.sources.count() else
                            "当前音源未提供发现内容；普通搜索、本地音乐、最近播放和我的歌单仍可使用。排行榜以音源实际支持为准。")
        if self.isVisible():
            self._source_changed()

    def show_view(self, view):
        if view == "history":
            self.navigate.emit(2)
            return
        self._task.invalidate()
        self._last_request = None
        self._sheet = None
        self.paging_changed.emit(False)
        self._view = view
        self.nav_buttons[view].setChecked(True)
        self.pages.setCurrentWidget(self.home if view == "home" else self.category_page)
        self.back.setVisible(view != "home")
        self.tags.setVisible(view in ("home", "sheets"))
        self.artist_controls.setVisible(view == "artists")
        self.more.hide()
        self.sheets.clear()
        self.empty.setText("")
        titles = {"ranks": "排行榜", "sheets": "精选与分类歌单", "new": "新歌 · 音源提供的新歌榜", "artists": "歌手与专辑"}
        self.category_title.setText(titles.get(view, ""))
        if view in ("home", "ranks", "new"):
            self._ensure_catalog()
        if view in ("home", "sheets"):
            self._load_sheets() if self.tags.count() else self._request_tags()
        if view in ("ranks", "new"):
            self._render_charts()
        if view == "artists":
            self._artist_kind_changed()
        if view == "home":
            self._refresh_local()

    def _source_changed(self, *_):
        self._task.invalidate()
        self._last_request = None
        self._sheet = None
        self._tracks = []
        self._track_pages.clear()
        self.paging_changed.emit(False)
        self.tags.blockSignals(True)
        self.tags.clear()
        self.tags.blockSignals(False)
        self.sheets.clear()
        self._render_sheets([])
        self._render_charts()
        if self._view in ("home", "ranks", "new"):
            self._ensure_catalog()
        if self._view in ("home", "sheets"):
            self._request_tags()
        elif self._view == "artists":
            self._artist_kind_changed()

    def _request_tags(self):
        caps = self._capabilities.get(self.sources.currentData(), {})
        if caps.get("sheets"):
            self._request("tags")
        else:
            self.status.setText("此音源未提供推荐歌单；可查看排行榜、最近播放或直接搜索。")
            self._render_sheets([])

    def _request(self, operation, *, force=False, **params):
        source = self.sources.currentData()
        if not source:
            return
        self._last_request = (operation, dict(params))
        cache_key = (source, operation, json.dumps(params, sort_keys=True, ensure_ascii=False, default=str))
        cached = None if force else self._browse_cache.get(cache_key)
        self.status.setText("正在读取音源内容…")
        self.more.setEnabled(False)
        view = self._view
        def read():
            if cached is not None and time.monotonic() - cached[0] < 120:
                return cached[1]
            if operation == "chart":
                result = self.music.get_top_list_detail(source, params["item"]["id"], page=params.get("page", 1))
            else:
                result = self.music.browse_online(source, operation, **params)
            if not isinstance(result, dict):
                raise ValueError("unconfirmed catalogue result")
            field = "tags" if operation == "tags" else "data" if operation in ("sheets", "artists") else "tracks"
            if not isinstance(result.get(field), list):
                raise ValueError("invalid catalogue list")
            return result
        self._task.submit((source, operation, view), read)

    def retry(self):
        if self._last_request:
            self._request(self._last_request[0], force=True, **self._last_request[1])
        elif self._view == "artists":
            self._find_artist()
        elif self._view in ("home", "ranks", "new"):
            # The forced catalogue read below is the only directory retry.
            # _source_changed() would submit another read before it.
            if self._view == "home":
                self._request_tags()
        else:
            self._source_changed()
        if self._view in ("home", "ranks", "new"):
            self._ensure_catalog(force=True)

    def _load_sheets(self, *_):
        tag = self.tags.currentData()
        if not isinstance(tag, dict) or not str(tag.get("id") or "").strip():
            self.status.setText("此音源未提供可浏览的公开分类；可使用搜索或排行榜。")
            return
        self._page = 1
        self._sheet = None
        self._request("sheets", tag=tag, page=1)

    def _open_sheet(self, item):
        self._sheet = item.data(Qt.UserRole)
        self._page, self._tracks = 1, []
        self._track_pages.clear()
        self._request("sheet", item=self._sheet, page=1)

    def _open_chart(self, item):
        row = item.data(Qt.UserRole)
        source = row["source_name"]
        index = self.sources.findData(source)
        if index >= 0 and index != self.sources.currentIndex():
            self.sources.setCurrentIndex(index)
        self._sheet = row
        self._page, self._tracks = 1, []
        self._track_pages.clear()
        self._request("chart", item=self._sheet, page=1)

    def _open_category_item(self, item):
        if self._view in ("ranks", "new"):
            self._open_chart(item)
        elif self._view == "artists":
            self._sheet = item.data(Qt.UserRole)
            self._page, self._tracks = 1, []
            self._track_pages.clear()
            self._request("album_tracks" if self.artist_kind.currentData() == "album" else "artist_tracks", item=self._sheet, page=1)
        else:
            self._open_sheet(item)

    def _more(self):
        if not self._last_request:
            return
        operation, params = self._last_request
        self._request(operation, **dict(params, page=self._page + 1))

    def _artist_kind_changed(self, *_):
        self._task.invalidate()
        self._last_request = None
        self.sheets.clear()
        kind = self.artist_kind.currentData()
        supported = bool(self._capabilities.get(self.sources.currentData(), {}).get(kind))
        self.artist_search.setEnabled(supported)
        self.empty.setText("输入名称后查找，双击条目查看曲目。" if supported else "当前音源不支持此类浏览；顶部普通歌曲搜索仍可使用。")

    def _find_artist(self):
        keyword = self.artist_query.text().strip()
        if not keyword or not self.artist_search.isEnabled():
            self.empty.setText("请先输入名称，并选择支持此功能的音源。")
            return
        self._page = 1
        self._request("artists", keyword=keyword, kind=self.artist_kind.currentData(), page=1)

    def _loaded(self, key, value, error):
        source, operation, view = key
        if source != self.sources.currentData() or view != self._view:
            return
        self.more.setEnabled(True)
        if error or not isinstance(value, dict):
            hint = "请求超时，请检查网络后重试或换一个音源。" if getattr(error, "category", "") == "timeout" else "音源未完成请求，请重试或换一个音源。"
            self.status.setText(hint)
            if self._view != "home":
                self.empty.setText(hint)
            self.paging_changed.emit(bool(self._tracks))
            notify("音乐发现未完成", hint, status="failed", source="music")
            return
        cache_key = (source, operation, json.dumps(self._last_request[1], sort_keys=True, ensure_ascii=False, default=str))
        self._browse_cache[cache_key] = (time.monotonic(), value)
        while len(self._browse_cache) > 32:
            self._browse_cache.pop(next(iter(self._browse_cache)))
        if operation == "tags":
            self.tags.blockSignals(True)
            self.tags.clear()
            seen = set()
            for tag in value.get("tags", []):
                if isinstance(tag, dict) and str(tag.get("id") or "").strip() and str(tag["id"]) not in seen:
                    seen.add(str(tag["id"]))
                    self.tags.addItem(str(tag.get("title") or "分类"), tag)
            self.tags.blockSignals(False)
            self._load_sheets()
            return
        self._page = self._last_request[1].get("page", 1)
        more = value.get("is_end") is False
        if operation in ("sheets", "artists"):
            rows = value.get("data")
            if not isinstance(rows, list):
                self._loaded(key, None, ValueError("invalid catalogue list"))
                return
            rows = [dict(row, source_name=source) for row in rows if isinstance(row, dict)]
            if operation == "sheets" and self._page == 1:
                self._render_sheets(rows)
            if self._view != "home":
                if self._page == 1:
                    self.sheets.clear()
                for row in rows:
                    from PySide6.QtWidgets import QListWidgetItem
                    from .discovery_cards import DISPLAY_ROLE
                    item = QListWidgetItem(str(row.get("title") or row.get("name") or "未命名"))
                    item.setSizeHint(self.sheets.gridSize())
                    item.setData(Qt.UserRole, row)
                    item.setData(DISPLAY_ROLE, music_card(row))
                    self.sheets.addItem(item)
                self.empty.setText("" if self.sheets.count() else "此分类暂无内容，试试其他分类或音源。")
                self.more.setVisible(more)
            self.status.setText(f"{source} · 已读取 {len(rows)} 个条目" + (" · 首页仅展示少量预览" if self._view == "home" else ""))
        else:
            tracks = value.get("tracks")
            if not isinstance(tracks, list):
                self._loaded(key, None, ValueError("invalid catalogue tracks"))
                return
            if self._page == 1:
                self._track_pages.clear()
            self._track_pages[self._page] = list(tracks)
            self._tracks = [track for page in sorted(self._track_pages) for track in self._track_pages[page]]
            title = str(self._sheet.get("title") or self._sheet.get("name") or "曲目")
            self.status.setText(f"{source} · {title} · {len(self._tracks)} 首")
            self.back.show()
            self.paging_changed.emit(more)
            self.tracks_ready.emit(list(self._tracks), title)
            notify("曲目已加载", self.status.text(), source="music", status="success" if self._tracks else "warning")

    def _render_sheets(self, rows):
        empty = "当前音源未提供公开推荐歌单，可换音源或直接搜索。"
        self.sections["today"].set_rows(rows[:6], music_card, empty)
        self.sections["featured"].set_rows(rows[6:12], music_card, "选择“歌单”可浏览其他分类。")

    def _ensure_catalog(self, force=False):
        getter = getattr(self.music, "get_top_lists", None)
        if not callable(getter):
            self._render_charts()
            return
        if not force and self._catalog is not None and time.monotonic() - self._catalog_at < 120:
            self._render_charts()
            return
        if self._catalog_task._running and not force:
            self._render_charts()
            return
        self._catalog_error = None
        self._catalog_task.submit("charts", getter)
        self._render_charts()

    def _catalog_loaded(self, key, result, error):
        if error or not isinstance(result, dict):
            self._catalog_error = getattr(error, "category", "unknown")
            self._render_charts()
            hint = self._catalog_failure_hint(self._catalog_error)
            notify("音乐榜单未完成", hint, source="music", status="failed")
            return
        self._catalog, self._catalog_at = result, time.monotonic()
        self._catalog_error = None
        self.sources.blockSignals(True)
        for source in result:
            if self.sources.findData(source) < 0:
                # A non-JS source may expose charts without plugin metadata.
                self.sources.addItem(source, source)
                self._capabilities.setdefault(source, {})["charts"] = True
        self.sources.blockSignals(False)
        self.refresh_button.setEnabled(bool(self.sources.count()))
        self._render_charts()
        errors = getattr(result, "errors", {})
        if errors:
            categories = set(errors.values())
            hint = self._catalog_failure_hint(next(iter(categories)) if len(categories) == 1 else "unknown")
            notify("音乐榜单部分可用" if result else "音乐榜单未完成", hint + " 已有结果保留。", source="music", status="warning" if result else "failed")

    @staticmethod
    def _catalog_failure_hint(category):
        # Only use the reported category, never infer a network failure from
        # an empty response or expose an exception's diagnostic text in the UI.
        reason = {"timeout": "榜单目录请求超时，请检查网络后刷新重试或换一个音源。",
                  "network": "榜单目录网络连接失败，请检查网络后刷新重试或换一个音源。",
                  "plugin": "音源插件读取榜单时出错，请刷新重试；仍失败可换一个音源或刷新插件。",
                  "unavailable": "音源暂不可用，请刷新插件后重试或换一个音源。"}
        return reason.get(category, "榜单目录读取未完成，原因尚未确认，请刷新重试或换一个音源。")

    def _catalog_hint(self, rows, *, new=False, has_charts=False):
        if self._catalog_task._running:
            return ("显示上次内容；" if rows else "") + "正在读取榜单目录，请稍候…"
        chosen = self.sources.currentData()
        errors = getattr(self._catalog, "errors", {})
        caps = self._capabilities.get(chosen, {})
        category = self._catalog_error or errors.get(chosen)
        bridge_errors = {name: value for name, value in errors.items() if name not in self._capabilities}
        if not category and chosen not in (self._catalog or {}) and bridge_errors and caps.get("charts") is not False:
            # A bridge-wide failure may not identify an individual plugin.
            # Its missing directory is unconfirmed, not proof of no support.
            categories = set(bridge_errors.values())
            category = next(iter(categories)) if len(categories) == 1 else "unknown"
        if category:
            prefix = ("显示上次内容；" if self._catalog_error else "部分内容可用；") if rows else ""
            return prefix + self._catalog_failure_hint(category)
        if rows:
            if errors:
                categories = set(errors.values())
                return "部分音源未完成；" + self._catalog_failure_hint(next(iter(categories)) if len(categories) == 1 else "unknown")
            return ""
        if caps.get("charts") is False:
            return "此音源明确不支持榜单功能，请换一个音源或直接搜索歌曲。"
        if self._catalog is None:
            return "榜单目录尚未读取，请刷新重试或直接搜索歌曲。"
        if new and has_charts:
            return "目录已读取，暂无可识别的新歌榜，可刷新或换一个音源。"
        return "目录已读取，暂无榜单内容，可稍后刷新或换一个音源。"

    def _render_charts(self):
        rows = []
        chosen = self.sources.currentData()
        for source, groups in (self._catalog or {}).items():
            if chosen and source != chosen:
                continue
            for group in groups:
                for row in group.get("lists", []):
                    rows.append(dict(row, source_name=source, cover_url=row.get("cover_img") or ""))
        fresh = [r for r in rows if any(mark in str(r.get("title", "")).casefold() for mark in ("新歌", "新曲", "new song", "new release"))]
        hint = self._catalog_hint(rows)
        new_hint = self._catalog_hint(fresh, new=True, has_charts=bool(rows))
        self.sections["ranks"].set_rows(rows[:6], music_card, hint)
        self.sections["new"].set_rows(fresh[:6], music_card, new_hint)
        if self._view in ("ranks", "new"):
            selected = fresh if self._view == "new" else rows
            hint = new_hint if self._view == "new" else hint
            self.sheets.set_rows(selected, music_card)
            self.empty.setText("" if selected else hint)
            self.status.setText(hint or f"{chosen} · 已读取 {len(selected)} 个榜单")

    def _refresh_local(self):
        def read():
            history = self.music.get_history(limit=8) if callable(getattr(self.music, "get_history", None)) else []
            playlists = self.music.get_playlists() if callable(getattr(self.music, "get_playlists", None)) else []
            return {"history": history if isinstance(history, list) else [], "playlists": playlists if isinstance(playlists, list) else []}
        self._local_task.submit("local", read)

    def _local_loaded(self, key, result, error):
        if error:
            self.sections["recent"].set_rows([], music_card, "最近播放暂不可读取，可稍后重试。")
            self.sections["mine"].set_rows([], music_card, "歌单暂不可读取，原有记录没有改变。")
            return
        self._history = result["history"]
        self._playlists = result["playlists"]
        self.sections["recent"].set_rows(self._history[:6], music_card, "还没有播放记录，先搜索或播放一首歌吧。")
        self.sections["mine"].set_rows(self._playlists[:6], music_card, "还没有歌单，可在现有歌单页创建。")
