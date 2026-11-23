"""Bounded home previews and on-demand categories inside the movie tab."""
from datetime import datetime
from copy import deepcopy
from html import unescape
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QButtonGroup,
    QComboBox, QScrollArea, QFileDialog, QLineEdit, QStackedWidget, QMenu)

from ...core.notifications import notify
from ...core.path_resolve import USER_DATA_DIR
from ...movie.discovery import MovieDiscovery, PLATFORMS, CATEGORIES, browse_works, official_url
from ...movie.library import MovieLibrary
from ..settings_store import load_ui_prefs, save_ui_prefs
from .latest_task import LatestTask
from .discovery_cards import CardList, ContentSection, label

KINDS = ("movie", "tv", "anime", "variety")


def work_summary(row):
    for key in ("description", "summary", "overview", "vod_content"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())[:100]
    return ""


def film_card(row):
    sources = row.get("sources") or []
    kind = CATEGORIES.get(row.get("kind"), "")
    year = str(row["year"]) if row.get("year") else ""
    progress = row.get("resume_seconds")
    subtitle = " · ".join(v for v in (year, kind) if v)
    if progress:
        minutes, seconds = divmod(int(progress), 60)
        subtitle = f"续播 {minutes}:{seconds:02d}"
    caption = " · ".join(str(tag) for tag in (row.get("genres") or [])[:3] if tag)
    caption = work_summary(row) or caption
    if type(row.get("score")) in (int, float) and row.get("score_source"):
        subtitle = " · ".join(v for v in (subtitle, f"{row['score_source']} {row['score']:g}分") if v)
    if row.get("recommendation_reason"):
        caption = row["recommendation_reason"] + (" · " + caption if caption else "")
    if not sources:
        caption = "本地影片" if row.get("available", True) else "文件暂不可访问"
    return dict(title=row.get("title", "未命名"), subtitle=subtitle, caption=caption,
                cover_url=row.get("cover_url") or "")


class MovieHome(QWidget):
    play_local = Signal(dict)
    play_history = Signal(dict)
    find_work = Signal(dict)
    show_history = Signal()
    resume_recommendation = Signal(dict)

    def __init__(self, movie, parent=None, library=None, discovery=None):
        super().__init__(parent)
        self.movie = movie
        self._closed = False
        self.library = library or MovieLibrary(USER_DATA_DIR / "data/movie/library.json")
        service = getattr(movie, "recommendations", None)
        self.catalog = discovery or (service.discovery if service is not None else None) or MovieDiscovery(USER_DATA_DIR / "data/movie/public_ranks.json")
        self._entries = list(self.library.entries)
        self._continuing = []
        self._selected_work = None
        self._view = "all"
        self._boards_loaded = False
        self._preview_result = None
        self._category_rows = []
        self._requested = None
        self._local_task = LatestTask(self)
        self._board_task = LatestTask(self)
        self._recommendation_task = LatestTask(self)
        self._preference_task = LatestTask(self)
        self._recommendation_task.completed.connect(self._recommendations_loaded)
        self._preference_task.completed.connect(self._preference_saved)
        self._local_task.completed.connect(self._local_loaded)
        self._board_task.completed.connect(self._boards_loaded_result)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.navigation = QButtonGroup(self)
        self.nav_buttons = {}
        for key, name in [("movie", "电影"), ("tv", "电视剧"), ("anime", "动漫"),
                          ("variety", "综艺"), ("all", "发现"), ("local", "本地影片")]:
            button = QPushButton(name)
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, kind=key: self.show_category(kind))
            self.navigation.addButton(button)
            self.nav_buttons[key] = button
            nav.addWidget(button)
        self.nav_buttons["all"].setChecked(True)
        nav.addStretch()
        self.official_button = QPushButton("官方观看入口")
        menu = QMenu(self.official_button)
        for platform in PLATFORMS:
            menu.addAction(platform.name + " · 官方入口").triggered.connect(
                lambda _=False, key=platform.key: self.open_official(key))
        self.official_button.setMenu(menu)
        nav.addWidget(self.official_button)
        root.addLayout(nav)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.home = QScrollArea()
        self.home.setWidgetResizable(True)
        self.home.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.home.setWidget(body)
        home_layout = QVBoxLayout(body)
        home_layout.setContentsMargins(8, 4, 8, 12)
        home_layout.setSpacing(18)
        intro = QHBoxLayout()
        self.preview_stamp = label("正在读取近期热门作品…", True)
        intro.addWidget(self.preview_stamp, 1)
        refresh = QPushButton("刷新首页")
        refresh.clicked.connect(lambda: self.refresh_boards(force=True))
        intro.addWidget(refresh)
        home_layout.addLayout(intro)
        recommendation_head = QHBoxLayout()
        self.recommendation_kind = QComboBox()
        for key in ("movie", "tv", "anime"):
            self.recommendation_kind.addItem(CATEGORIES[key], key)
        self.recommend_button = QPushButton("为我推荐")
        self.recommend_button.clicked.connect(self.request_recommendations)
        self.continue_show_button = QPushButton("继续观看")
        self.continue_show_button.clicked.connect(lambda: self.request_recommendations(resume=True))
        recommendation_head.addWidget(self.recommendation_kind)
        recommendation_head.addWidget(self.recommend_button)
        recommendation_head.addWidget(self.continue_show_button)
        recommendation_head.addStretch()
        self.recommendation_section = ContentSection("猜你喜欢", self, portrait=True, action="换一批")
        self.recommendation_section.all_button.clicked.connect(self.request_recommendations)
        self.recommendation_section.cards.itemClicked.connect(self._select_work)
        self.recommendation_section.cards.itemDoubleClicked.connect(self._activate_recommendation)
        self.recommendation_section.set_rows([], film_card, "点击为我推荐：参考实际观看偏好和可用热榜，不会自动播放。")
        self.continue_section = ContentSection("继续观看", self, portrait=True)
        self.continue_section.all_button.clicked.connect(lambda: self.show_category("continue"))
        self.continue_list = self.continue_section.cards
        self.continue_list.itemDoubleClicked.connect(self._activate_continue)
        self.continue_hint = self.continue_section.hint
        self.recent_section = ContentSection("最近添加", self, portrait=True)
        self.recent_section.all_button.clicked.connect(lambda: self.show_category("recent"))
        self.local_list = self.recent_section.cards
        self.local_list.itemDoubleClicked.connect(lambda item: self.play_local.emit(item.data(Qt.UserRole)))
        home_layout.addWidget(self.continue_section)
        self.board_status = label("正在读取公开榜单预览，本地影片不受影响…", True)
        self.sections = {}
        for kind in KINDS:
            section = ContentSection("热门" + CATEGORIES[kind], self, portrait=True)
            section.all_button.clicked.connect(lambda _=False, k=kind: self.show_category(k))
            section.cards.itemClicked.connect(self._select_work)
            section.cards.itemDoubleClicked.connect(self._activate_discovery_item)
            section.cards.setToolTip("单击查看详情，双击按片名搜索；选择搜索结果后再播放。")
            section.set_rows([], film_card, "正在读取预览…")
            self.sections[kind] = section
            home_layout.addWidget(section)
        home_layout.addLayout(recommendation_head)
        home_layout.addWidget(self.recommendation_section)
        home_layout.addWidget(self.recent_section)
        home_layout.addWidget(self.board_status)
        local_head = QHBoxLayout()
        local_head.addWidget(label("我的本地电影库"), 1)
        self.add_button = QPushButton("添加影片文件夹")
        self.add_button.clicked.connect(self.add_folder)
        self.scan_button = QPushButton("刷新电影库")
        self.scan_button.clicked.connect(lambda: self.refresh_local(scan=True))
        all_local = QPushButton("查看全部")
        all_local.clicked.connect(lambda: self.show_category("local"))
        for button in (self.add_button, self.scan_button, all_local):
            local_head.addWidget(button)
        home_layout.addLayout(local_head)
        self.local_status = label("还没有影片文件夹。添加一次即可建立索引，不移动或复制影片。", True)
        home_layout.addWidget(self.local_status)
        home_layout.addStretch()
        self.pages.addWidget(self.home)

        self.category_page = QWidget()
        category_layout = QVBoxLayout(self.category_page)
        category_layout.setContentsMargins(8, 4, 8, 8)
        head = QHBoxLayout()
        self.category_title = label()
        self.category_title.setProperty("class", "title")
        head.addWidget(self.category_title, 1)
        self.retry_button = QPushButton("刷新此分类")
        self.retry_button.clicked.connect(lambda: self.refresh_boards(force=True))
        head.addWidget(self.retry_button)
        category_layout.addLayout(head)
        filters = QHBoxLayout()
        self.local_search = QLineEdit()
        self.local_search.setPlaceholderText("筛选片名")
        self.source_filter = QComboBox()
        self.source_filter.setToolTip("只筛选真正提供当前榜单数据的来源；不改变播放片源")
        self.order = QComboBox()
        self.order.addItems(["榜单顺序", "名称", "年份"])
        filters.addWidget(self.local_search, 1)
        self.filter_button = QPushButton("来源筛选")
        self.filter_button.setCheckable(True)
        self.filter_button.toggled.connect(self._toggle_sources)
        filters.addWidget(self.filter_button)
        filters.addWidget(self.source_filter)
        filters.addWidget(self.order)
        category_layout.addLayout(filters)
        self.category_status = label("", True)
        category_layout.addWidget(self.category_status)
        self.boards = CardList(self, portrait=True)
        self.boards.itemClicked.connect(self._select_work)
        self.boards.itemDoubleClicked.connect(self._activate_category_item)
        category_layout.addWidget(self.boards, 1)
        self.category_empty = label("", True)
        category_layout.addWidget(self.category_empty)
        self.pages.addWidget(self.category_page)
        self.local_search.textChanged.connect(self._render_category)
        self.source_filter.currentIndexChanged.connect(self._render_category)
        self.order.currentIndexChanged.connect(self._render_category)

        self.detail = QWidget()
        detail_layout = QHBoxLayout(self.detail)
        detail_layout.setContentsMargins(8, 4, 8, 4)
        self.work_detail = label()
        detail_layout.addWidget(self.work_detail, 1)
        self.watch_button = QPushButton("官方观看")
        self.watch_button.clicked.connect(lambda: self.open_official())
        detail_layout.addWidget(self.watch_button)
        self.like_button = QPushButton("喜欢这部")
        self.reduce_button = QPushButton("不感兴趣")
        self.like_button.clicked.connect(lambda: self.remember_selected(True))
        self.reduce_button.clicked.connect(lambda: self.remember_selected(False))
        detail_layout.addWidget(self.like_button)
        detail_layout.addWidget(self.reduce_button)
        root.addWidget(self.detail)
        self.detail.hide()
        self._render_local()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_local()
        if not self._boards_loaded and self._view == "all" and self._requested is None:
            self.refresh_boards()

    def shutdown(self):
        self._closed = True
        self._recommendation_task.cancel()
        self._preference_task.cancel()
        service = getattr(self.movie, "recommendations", None)
        if service is not None:
            service.cancel()
        self._local_task.cancel()
        self._board_task.cancel()

    def show_category(self, kind):
        if kind not in (*CATEGORIES, "local", "recent", "continue"):
            return
        self._recommendation_task.invalidate()
        service = getattr(self.movie, "recommendations", None)
        if service is not None:
            service.cancel()
        self._board_task.invalidate()
        self._requested = None
        self._view = kind
        self.movie.voice_browse_kind = kind if kind in KINDS else ""
        self._selected_work = None
        self.detail.hide()
        self.nav_buttons[kind if kind in self.nav_buttons else "all"].setChecked(True)
        if kind == "all":
            self.pages.setCurrentWidget(self.home)
            if self._preview_result is not None:
                self._render_previews(self._preview_result)
            else:
                self.refresh_boards()
            return
        self.pages.setCurrentWidget(self.category_page)
        self.filter_button.setVisible(kind in KINDS)
        self.filter_button.setChecked(False)
        self.source_filter.setVisible(kind not in KINDS)
        self.category_title.setText({"local": "我的本地电影库", "recent": "最近添加", "continue": "继续观看"}.get(kind, CATEGORIES.get(kind)))
        self.local_search.blockSignals(True)
        self.local_search.clear()
        self.local_search.blockSignals(False)
        self.order.blockSignals(True)
        self.order.setCurrentIndex(0)
        self.order.setItemText(0, "最近添加" if kind in ("local", "recent") else "最近观看" if kind == "continue" else "综合浏览")
        self.order.blockSignals(False)
        self.boards.clear()
        self._category_rows = []
        self.boards.setToolTip("双击播放本地影片。" if kind in ("local", "recent", "continue") else
                               "单击查看详情，双击按片名搜索；选择搜索结果后再播放。")
        if kind in ("local", "recent", "continue"):
            self._set_local_category()
        else:
            self.refresh_boards()

    def request_recommendations(self, _checked=False, *, resume=False):
        service = getattr(self.movie, "recommendations", None)
        if service is None:
            self.recommendation_section.hint.setText("电影推荐尚未就绪，请稍后再试")
            return
        kind = self.recommendation_kind.currentData()
        if resume and kind == "movie":
            kind = "tv"
        self.recommendation_section.hint.setText("正在查找续播记录…" if resume else "正在参考偏好和公开榜单选片…")
        self.recommendation_section.hint.show()
        read = service.continuing if resume else service.recommend
        service.cancel()
        self._recommendation_task.submit((kind, resume), lambda: read(kind))

    def _recommendations_loaded(self, key, result, error):
        if error or not isinstance(result, tuple):
            self.recommendation_section.hint.setText("推荐暂时未完成，请重试；本地影片和播放历史仍可使用")
            return
        batch, current = result
        if current():
            self.present_recommendations(batch)
            if batch.action == "resume" and len(batch.rows) == 1:
                self.resume_recommendation.emit(batch.rows[0])

    def present_recommendations(self, batch):
        if self._closed:
            return
        self._selected_work = None
        self.detail.hide()
        self._view = "all"
        self.nav_buttons["all"].setChecked(True)
        self.pages.setCurrentWidget(self.home)
        index = self.recommendation_kind.findData(batch.kind)
        if index >= 0:
            self.recommendation_kind.setCurrentIndex(index)
        self.recommendation_section.title.setText("继续观看" if batch.action == "resume" else "猜你喜欢 · " + CATEGORIES.get(batch.kind, "影视"))
        publish = getattr(self.movie, "publish_voice_candidates", None)
        rows = publish(batch.rows) if callable(publish) else batch.rows
        self.movie.voice_browse_kind = batch.kind if batch.kind in KINDS else ""
        self.recommendation_section.set_rows(rows, film_card, batch.message)
        self.recommendation_section.hint.setText(batch.message)
        self.recommendation_section.hint.show()

    def _activate_recommendation(self, item):
        row = item.data(Qt.UserRole)
        if isinstance(row, dict) and row.get("_resume_item"):
            self.resume_recommendation.emit(row)
        elif isinstance(row, dict) and row.get("_history_row"):
            self.play_history.emit(row["_history_row"])
        else:
            self._activate_discovery_item(item)

    def _activate_continue(self, item):
        row = item.data(Qt.UserRole)
        if isinstance(row, dict) and row.get("_resume_item"):
            self.resume_recommendation.emit(row)
        else:
            self.play_history.emit(row)

    def remember_selected(self, positive):
        row = deepcopy(self._selected_work)
        service = getattr(self.movie, "recommendations", None)
        if not row or service is None:
            return
        self.like_button.setEnabled(False)
        self.reduce_button.setEnabled(False)
        self.recommendation_section.hint.setText("正在保存偏好…")
        self._preference_task.submit(positive, lambda: service.feedback(row, positive))

    def _preference_saved(self, positive, result, error):
        self.like_button.setEnabled(True)
        self.reduce_button.setEnabled(True)
        message = ("已记住你的喜好" if positive else "后续推荐将避开这部作品") if result is True and not error else "偏好未能保存，请检查数据目录后重试"
        self.recommendation_section.hint.setText(message)
        notify(message, source="movie", status="success" if result is True and not error else "failed")

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择影片文件夹")
        if not path:
            return
        paths = list(load_ui_prefs().get("movie_library_dirs") or [])
        if path not in paths:
            paths.append(path)
        if not save_ui_prefs({"movie_library_dirs": paths}):
            self.local_status.setText("目录未能保存，请检查用户配置目录权限。")
            notify("影片目录未保存", self.local_status.text(), source="movie", status="failed")
            return
        self.refresh_local(scan=True)

    def refresh_local(self, scan=False):
        directories = list(load_ui_prefs().get("movie_library_dirs") or [])
        watch = getattr(getattr(self.movie, "mpv", None), "watch_later_dir", None)
        watch = watch if isinstance(watch, (str, Path)) else None
        self.local_status.setText("正在扫描文件名…" if scan else "正在读取电影库…")
        def read():
            result = self.library.scan(directories) if scan else {"entries": list(self.library.entries), "issues": []}
            history = self.movie.get_history() if self.movie is not None else []
            legacy = MovieLibrary.continuing(history if isinstance(history, list) else [], watch)
            service = getattr(self.movie, "recommendations", None)
            remembered = [r for kind in ("movie", "tv", "anime") for r in service.resume_rows(kind)] if service is not None else []
            paths = {r["_resume_item"].get("first_url") for r in remembered}
            continuing = remembered + [r for r in legacy if r.get("replay_value") not in paths]
            continuing.sort(key=lambda r: r.get("updated", r.get("played_at", 0)), reverse=True)
            return dict(result, continuing=continuing)
        self._local_task.submit("scan" if scan else "read", read)

    def _local_loaded(self, key, result, error):
        if error:
            self.local_status.setText("电影库未能读取，请检查磁盘或目录权限后刷新；原文件未改变。")
            notify("电影库未完成", self.local_status.text(), source="movie", status="failed")
            return
        self._entries = result["entries"]
        self._continuing = result.get("continuing", [])
        self._render_local()
        if result.get("issues"):
            self.local_status.setText("；".join(result["issues"]) + "。已有记录保留。")
        if self._view in ("local", "recent", "continue"):
            self._set_local_category()
        if key == "scan":
            notify("电影库部分更新" if result.get("issues") else "电影库已更新", self.local_status.text(),
                   source="movie", status="warning" if result.get("issues") else "success")

    def _render_local(self):
        self.continue_section.set_rows(self._continuing[:3], film_card, "暂无续播记录；有可靠进度的影片或剧集会出现在这里。")
        available = [r for r in self._entries if r.get("available", True)]
        self.recent_section.set_rows(sorted(available, key=lambda r: r.get("added_at", 0), reverse=True)[:6], film_card,
                                     "还没有本地影片，点击下方添加文件夹即可。")
        self.local_status.setText(f"已收录 {len(self._entries)} 部 · {len(available)} 部可访问。可在本地分类中搜索和筛选。" if self._entries
                                 else "还没有影片文件夹。添加一次即可建立索引，不移动或复制影片。")

    def _set_filters(self, options):
        self.source_filter.blockSignals(True)
        self.source_filter.clear()
        for text, key in options:
            self.source_filter.addItem(text, key)
        self.source_filter.blockSignals(False)

    def _set_local_category(self):
        self._category_rows = list(self._continuing if self._view == "continue" else self._entries)
        if self._view == "recent":
            self._category_rows.sort(key=lambda r: r.get("added_at", 0), reverse=True)
        self._set_filters([("全部文件", "*"), ("可访问", "available"), ("暂不可访问", "offline")])
        self.category_status.setText("本地内容 · 双击播放，不依赖在线榜单。")
        self._render_category()

    def show_local_matches(self, work):
        rows = self.library.find(work)
        if rows:
            self.show_category("local")
            self._category_rows = rows
            self.local_status.setText(f"本地找到 {len(rows)} 个版本，请核对年份后双击播放。")
            self.category_status.setText(self.local_status.text())
            self._render_category()
        return rows

    def refresh_boards(self, force=False):
        kind = self._view
        if kind in ("local", "recent", "continue"):
            self.refresh_local(scan=force and kind != "continue")
            return
        if self._requested == kind and not force:
            return
        self._requested = kind
        target = self.board_status if kind == "all" else self.category_status
        target.setText("正在读取分类预览…" if kind == "all" else "正在读取此分类；本地影片不受影响…")
        self._board_task.submit(kind, lambda: self.catalog.read("all", force=force, limit=6) if kind == "all"
                                else self.catalog.read(kind, force=force))

    @staticmethod
    def _status(result):
        states = result.get("states", [])
        times = [s["at"] for s in states if s.get("at")]
        stamp = datetime.fromtimestamp(min(times)).strftime("%m-%d %H:%M") if times else "尚无成功数据"
        names = list(dict.fromkeys(s.get("platform", "") for s in states if s.get("platform")))
        text = "榜单来源：" + ("、".join(names) or "尚未接入此分类") + " · 读取于 " + stamp
        failed = [s for s in states if s.get("state") in ("failed", "stale")]
        reasons = {"timeout": "请求超时", "network": "连接失败", "response": "内容暂不可读取",
                   "http": "平台暂不可访问", "service_rejected": "平台暂未接受请求"}
        for state in failed:
            text += "；" + state["platform"] + " " + CATEGORIES.get(state["kind"], "") + "：" + reasons.get(state.get("error"), "未能更新")
        if any(s["state"] == "stale" for s in states):
            text += " · 含过期缓存"
        return text, failed

    def _boards_loaded_result(self, kind, result, error):
        if kind != self._view:
            return
        self._requested = None
        if error or not isinstance(result, dict):
            hint = "榜单暂不可用，请刷新重试或在平台官网查看；本地影片不受影响。"
            (self.board_status if kind == "all" else self.category_status).setText(hint)
            if kind == "all":
                self.preview_stamp.setText(hint)
                for section in self.sections.values():
                    section.set_rows([], film_card, hint)
            else:
                self.boards.clear()
                self.category_empty.setText(hint)
            notify("影视榜单未完成", hint, source="movie", status="failed")
            return
        status, failed = self._status(result)
        if kind == "all":
            stamps = [s["at"] for s in result.get("states", []) if s.get("at")]
            brief = "更新于 " + datetime.fromtimestamp(min(stamps)).strftime("%m-%d %H:%M") if stamps else ""
            if failed:
                brief += " · 含过期缓存" if any(s["state"] == "stale" for s in failed) else " · 部分榜单未能更新"
            self.preview_stamp.setText(brief.strip(" ·"))
            self.preview_stamp.setToolTip(status)
            self._boards_loaded = True
            self._preview_result = result
            links = "、".join(p.name for p in result.get("official_only", [p for p in PLATFORMS if not p.automatic]))
            self.board_status.setText("发现喜欢的作品后，可按片名搜索；官方观看入口单独保留。")
            self.board_status.setToolTip(status + ("；官方入口：" + links if links else ""))
            self._render_previews(result)
        else:
            self._category_rows = [r for r in result.get("works", []) if r.get("kind") == kind]
            sources = {}
            for row in self._category_rows:
                for source in row.get("sources", []):
                    sources[source["platform"]] = source["platform_name"]
            self._set_filters([("全部榜单来源", "*"), *[(name, key) for key, name in sources.items()]])
            self.category_status.setToolTip(status)
            brief = ("部分内容未能更新，可刷新重试" if failed else f"{len(self._category_rows)} 部 · 综合浏览")
            self.category_status.setText(brief + "。榜单信息不代表可直接播放。")
            self._render_category()
        found = bool(result.get("works"))
        title = ("影视榜单部分可用" if found else "影视榜单未完成") if failed else (
            "影视榜单已更新" if found else "此分类尚未接入" if not result.get("states") else "此分类暂无榜单内容")
        notify(title, status, source="movie", status=("warning" if found else "failed") if failed else "success" if found else "warning")

    def _render_previews(self, result):
        self._selected_work = None
        self.detail.hide()
        for kind, section in self.sections.items():
            rows = [r for r in result.get("works", []) if r.get("kind") == kind][:6]
            states = [s for s in result.get("states", []) if s.get("kind") == kind]
            failure = any(s.get("state") in ("failed", "stale") for s in states)
            empty = "此分类未能读取，点击查看全部后可刷新重试。" if failure else "这个分类暂无榜单内容。" if states else "暂无内容，可搜索片名或使用官方观看入口。"
            section.set_rows(rows, film_card, empty)

    def _toggle_sources(self, expanded):
        self.source_filter.setVisible(expanded)
        if not expanded:
            self.source_filter.setCurrentIndex(0)

    def _render_category(self, *_):
        self._selected_work = None
        self.detail.hide()
        query = self.local_search.text().strip().casefold()
        chosen = self.source_filter.currentData()
        rows = [r for r in self._category_rows if not query or query in str(r.get("title", "")).casefold()]
        if self._view in ("local", "recent", "continue"):
            if chosen in ("available", "offline"):
                rows = [r for r in rows if bool(r.get("available", True)) == (chosen == "available")]
        else:
            rows = browse_works(rows, None if chosen in (None, "*") else chosen)
        if self.order.currentIndex() == 1:
            rows = sorted(rows, key=lambda r: str(r.get("title", "")).casefold())
        elif self.order.currentIndex() == 2:
            rows = sorted(rows, key=lambda r: r.get("year") if type(r.get("year")) is int else -1, reverse=True)
        self.boards.set_rows(rows, film_card)
        self.category_empty.setText("" if rows else "未找到匹配内容，可调整筛选或刷新；本地影片也可从顶部直接打开。")
        self.category_empty.setVisible(not rows)
        self.boards.setVisible(bool(rows))

    def _select_work(self, item, *_):
        self._selected_work = item.data(Qt.UserRole) if item else None
        remember = getattr(self.movie, "note_voice_candidate", None)
        if callable(remember):
            remember(self._selected_work.get("_voice_candidate_id") if isinstance(self._selected_work, dict) else None)
        row = self._selected_work
        self.detail.setVisible(bool(row))
        if not row:
            return
        data = film_card(row)
        ranks = "；".join(str(s.get("platform_name") or "观看记录")
            + (" " + str(s["board"]) if s.get("board") else "")
            + (f"：第 {s['rank']} 名" if type(s.get("rank")) is int else "") for s in row.get("sources", []))
        updates = list(dict.fromkeys(s["source_updated"] for s in row.get("sources", []) if s.get("source_updated")))
        self.work_detail.setText(" · ".join(v for v in (data["title"], data["subtitle"]) if v)
                                + ("\n榜单出处：" + ranks if ranks else "")
                                + ("\n" + "；".join(updates) if updates else "")
                                + ("\n榜单仅供发现内容，官方页面可能需要会员或地区权限。" if ranks else ""))
        self.watch_button.setVisible(any(official_url(s.get("url"), s.get("platform")) for s in row.get("sources", [])))
        self.watch_button.setText("官方搜索" if row.get("url_kind") == "search" else "官方观看")

    def _activate_category_item(self, item):
        row = item.data(Qt.UserRole)
        if self._view == "continue":
            self._activate_continue(item)
        elif self._view in ("local", "recent"):
            self.play_local.emit(row)
        else:
            self._activate_discovery_item(item)

    def _activate_discovery_item(self, item):
        # The card carries its provider IDs/URL and metadata. Never look it up
        # by a row number that filtering, sorting or refresh may have changed.
        row = item.data(Qt.UserRole) if item is not None else None
        if isinstance(row, dict) and isinstance(row.get("title"), str) and row["title"].strip():
            self.find_work.emit(dict(row))

    def open_official(self, key=None):
        row = self._selected_work
        if key is None and row and row.get("sources"):
            key = row["sources"][0]["platform"]
        if key is None:
            return
        url = next((s["url"] for s in row.get("sources", []) if s["platform"] == key), "") if row else ""
        fallback = next((p.url for p in PLATFORMS if p.key == key), "")
        url = official_url(url or fallback, key)
        if not url or not QDesktopServices.openUrl(QUrl(url)):
            notify("官网未能打开", "请检查默认浏览器，或稍后重试。", source="movie", status="failed")
