"""Immersive lyrics driven by the shared LyricsWindow clock."""
from PySide6.QtCore import QAbstractAnimation, QEvent, QRectF, Qt, QTimer, Signal, QVariantAnimation
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QAbstractItemView, QBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QStackedWidget, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from ..settings_store import load_lyrics_prefs
from ..theme.motion import DUR_EXIT, EASE_IN_OUT, EASE_OUT, reduced_motion_enabled
from ..theme.tokens import Tokens
from .lyric_stage import LyricStage, STAGE_STYLES
from .lyric_stage_effects import DEFAULT_EFFECT, TEXT_EFFECTS
from .music_card_delegate import cover_key_of, _get_cover_loader


class _LyricLineDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        font = index.data(Qt.FontRole)
        if font is not None and font.bold():
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(Tokens.ACCENT))
            rect = option.rect
            painter.drawRoundedRect(QRectF(rect.left() + 5, rect.center().y() - 13, 3, 26), 1.5, 1.5)
            painter.restore()


class PlayingLyrics(QWidget):
    close_requested = Signal()
    style_chosen = Signal(str)
    effect_chosen = Signal(str)
    MAX_BACKDROP_PIXELS = 1_150_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = None
        self._auto_reader = False
        initial = load_lyrics_prefs()
        self._initial_reduced = initial.get("reduce_motion") is True
        self._initial_low_power = initial.get("stage_low_power") is True
        self.active_line = -1
        self._follow = True
        self._cover_key = None
        self._loader = None
        self._art_source = QPixmap()
        self._wash = QPixmap()
        self._previous_wash = QPixmap()
        self._cover_mix = 1.0
        self._backdrop_key = None
        self._backdrop = QPixmap()
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Tokens.SPACING_XL, Tokens.SPACING_MD, Tokens.SPACING_XL, Tokens.SPACING_MD)
        layout.setSpacing(Tokens.SPACING_MD)
        header = QHBoxLayout()
        self.tools = QHBoxLayout()
        self.btn_close = QPushButton("返回浏览")
        self.btn_close.setToolTip("返回原来的音乐浏览位置")
        self.btn_close.clicked.connect(self.close_requested.emit)
        header.addWidget(self.btn_close)
        self.style_button = QPushButton("三维悬浮")
        self.style_button.setAccessibleName("歌词舞台")
        self.style_menu = QMenu(self.style_button)
        self.style_actions = {}
        for key, label in STAGE_STYLES:
            action = self.style_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, style=key: self._choose_stage(style))
            self.style_actions[key] = action
        self.style_menu.addSeparator()
        self.reader_action = self.style_menu.addAction("完整歌词", self._open_reader)
        self.style_button.setMenu(self.style_menu)
        header.addWidget(self.style_button)
        self.effect_button = QPushButton("文字特效")
        self.effect_menu = QMenu(self.effect_button)
        self.effect_actions = {}
        for key, label in TEXT_EFFECTS:
            if key == "off":
                self.effect_menu.addSeparator()
            action = self.effect_menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, effect=key: self._choose_effect(effect))
            self.effect_actions[key] = action
        self.effect_button.setMenu(self.effect_menu)
        header.addWidget(self.effect_button)
        header.addStretch()
        header.addLayout(self.tools)
        layout.addLayout(header)

        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        warm_graphics = not (self._initial_reduced or initial.get("stage_low_power") or reduced_motion_enabled())
        self.stage = LyricStage(self.pages, warm_graphics=warm_graphics)
        self.pages.addWidget(self.stage)
        self.reader = QWidget()
        self.reader.setObjectName("LyricsReader")
        self.reader.setStyleSheet("#LyricsReader { background: transparent; }")
        self.pages.addWidget(self.reader)
        self.body = QBoxLayout(QBoxLayout.LeftToRight, self.reader)
        self.body.setSpacing(Tokens.SPACING_XL)
        self.identity = QWidget(self)
        self.identity.setObjectName("LyricsIdentity")
        self.identity.setStyleSheet("#LyricsIdentity { background: transparent; }")
        titles = QVBoxLayout(self.identity)
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(Tokens.SPACING_SM)
        self.art = QLabel("♪")
        self.art.setAlignment(Qt.AlignCenter)
        self.art.setFixedSize(176, 176)
        titles.addWidget(self.art)
        self.title = QLabel("歌词")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setProperty("class", "title")
        self.title.setStyleSheet(f"font-size: {Tokens.FONT_SIZE_HERO}px;")
        self.title.setWordWrap(True)
        titles.addWidget(self.title)
        self.artist = QLabel()
        self.artist.setTextFormat(Qt.PlainText)
        self.artist.setWordWrap(True)
        self.artist.setProperty("class", "secondary")
        titles.addWidget(self.artist)
        self.body.addWidget(self.identity, 0, Qt.AlignVCenter)
        self.follow = QPushButton("当前行")
        self.follow.setAccessibleName("回到当前行")
        self.follow.setToolTip("回到当前行并继续跟随播放进度")
        self.follow.clicked.connect(self._resume_follow)
        self.tools.addWidget(self.follow)
        self.message = QLabel("播放歌曲后显示歌词")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.body.addWidget(self.message, 1)
        self.lines = QListWidget()
        self.lines.setWordWrap(True)
        self.lines.setTextElideMode(Qt.ElideNone)
        self.lines.setResizeMode(QListWidget.Adjust)
        self.lines.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.lines.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lines.setSpacing(Tokens.SPACING_XS)
        self.lines.setItemDelegate(_LyricLineDelegate(self.lines))
        self.lines.setSelectionMode(QAbstractItemView.NoSelection)
        self.lines.setFocusPolicy(Qt.StrongFocus)
        self.lines.viewport().installEventFilter(self)
        self.body.addWidget(self.lines, 1)
        self._scroll = QVariantAnimation(self)
        self._scroll.setDuration(DUR_EXIT)
        self._scroll.setEasingCurve(EASE_OUT)
        self._scroll.valueChanged.connect(self.lines.verticalScrollBar().setValue)
        self.lines.verticalScrollBar().sliderPressed.connect(self._pause_follow)
        self.lines.verticalScrollBar().actionTriggered.connect(self._pause_follow)
        self.lines.itemClicked.connect(self._pause_follow)
        self.lines.hide()
        self.follow.hide()
        self.lines.setStyleSheet("QListWidget {background: transparent; border: none;}"
                                "QListWidget::item {background: transparent; padding: 10px 12px 10px 22px;}")
        self.art.setStyleSheet("background: transparent; color: palette(placeholder-text); font-size: 42px;")
        self._cover_wait = QTimer(self)
        self._cover_wait.setSingleShot(True)
        self._cover_wait.setInterval(260)
        self._cover_wait.timeout.connect(self._clear_wash)
        self._cover_fade = QVariantAnimation(self)
        self._cover_fade.setDuration(DUR_EXIT)
        self._cover_fade.setEasingCurve(EASE_IN_OUT)
        self._cover_fade.setStartValue(0.0)
        self._cover_fade.setEndValue(1.0)
        self._cover_fade.valueChanged.connect(self._set_cover_mix)
        self._cover_fade.finished.connect(self._finish_cover_fade)

    def attach(self, engine):
        self.engine = engine
        engine.lyrics_changed.connect(self.refresh)
        engine.appearance_changed.connect(self._restyle)
        engine.line_changed.connect(self.highlight)
        engine.clock_sampled.connect(self.sync_transport)

    def _choose_stage(self, key):
        self._scroll.stop()
        self.pages.setCurrentWidget(self.stage)
        self.style_chosen.emit(key)
        self.refresh()

    def _choose_effect(self, key):
        self.effect_chosen.emit(key)
        self._configure_effect()

    def _configure_effect(self):
        self.stage.set_effect(self.engine._prefs.get("stage_effect", DEFAULT_EFFECT))
        for key, action in self.effect_actions.items():
            action.setChecked(key == self.stage.effect)
        active = self.effect_actions[self.stage.effect].text()
        self.effect_button.setToolTip(active)
        self.effect_button.setAccessibleDescription(active)

    def _open_reader(self):
        self._auto_reader = False
        self.pages.setCurrentWidget(self.reader)
        self.refresh()

    def _configure_stage(self):
        prefs = self.engine._prefs
        key = prefs.get("stage_style", "space")
        mode = next((i for i, (name, _) in enumerate(STAGE_STYLES) if key == name), 0)
        quality = 2 if self.motion_reduced() else int(prefs.get("stage_low_power") is True)
        try:
            size = max(12, min(48, int(prefs.get("font_size") or 24)))
        except (TypeError, ValueError):
            size = 24
        self.stage.configure(mode, quality, str(prefs.get("font") or "Microsoft YaHei UI"), size)
        self._configure_effect()
        reading = self.pages.currentWidget() is self.reader
        self.effect_button.setVisible(not reading)
        self.style_button.setText("完整歌词" if reading else STAGE_STYLES[mode][1])
        for i, (name, _) in enumerate(STAGE_STYLES):
            self.style_actions[name].setChecked(not reading and i == mode)

    def refresh(self):
        if self.engine is None or self.isHidden():
            return
        self._follow = True
        self.active_line = self.engine._current_idx if self.engine._lines else -1
        self._scroll.stop()
        self.lines.clear()
        status = self.engine.lyric_status
        track = getattr(self.engine, "current_track", {})
        key = cover_key_of(track) if track.get("cover_url") or not track.get("source_name") else None
        if key and key.startswith(("local:http://", "local:https://")):
            key = None
        self._set_cover_key(key)
        self.artist.setText(track.get("artist", ""))
        self.artist.setVisible(bool(self.artist.text()))
        title = self.engine.loading_title if status == "loading" else self.engine._title
        self.title.setText(title or "歌词")
        if status == "plain" and self.pages.currentWidget() is self.stage:
            self.pages.setCurrentWidget(self.reader)
            self._auto_reader = True
        elif self._auto_reader and status == "ready":
            self.pages.setCurrentWidget(self.stage)
            self._auto_reader = False
        self._configure_stage()
        self.stage.bind(self.engine)
        reading = self.pages.currentWidget() is self.reader
        available = status in ("ready", "plain")
        self.message.setVisible(not available)
        self.lines.setVisible(available)
        self.follow.setVisible(status == "ready" and reading)
        if available and reading:
            text_rows = self.engine._plain_lines if status == "plain" else (text for _, text in self.engine._lines)
            for row, text in enumerate(text_rows):
                item = QListWidgetItem(text)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.lines.addItem(item)
                self._style(row)
            self.lines.doItemsLayout()
            if status == "ready":
                self.highlight(self.engine._current_idx, animate=False)
        elif not available:
            messages = {"idle": "播放歌曲后显示歌词", "loading": "正在查找歌词…", "missing": "这首歌暂无歌词"}
            self.message.setText(messages[status])

    def highlight(self, row, *, animate=True):
        if self.isHidden():
            return
        if self.pages.currentWidget() is self.stage:
            self.active_line = row
            self.stage.sync()
            return
        if not 0 <= row < self.lines.count():
            return
        previous = self.active_line
        self.active_line = row
        affected = set(range(previous - 1, previous + 2)) | set(range(row - 1, row + 2))
        for index in affected:
            if 0 <= index < self.lines.count():
                self._style(index)
        if self._follow:
            self._scroll_to_line(self.lines.item(row), animate)

    def _scroll_to_line(self, item, animate):
        bar = self.lines.verticalScrollBar()
        self._scroll.stop()
        if not animate or not self.engine.is_playing() or self.motion_reduced():
            self.lines.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            return
        middle = self.lines.visualItemRect(item).center().y()
        target = max(bar.minimum(), min(bar.maximum(),
                     bar.value() + middle - self.lines.viewport().height() // 2))
        if bar.value() != target:
            self._scroll.setStartValue(bar.value())
            self._scroll.setEndValue(target)
            self._scroll.start()

    def _style(self, row):
        item = self.lines.item(row)
        distance = abs(row - self.active_line) if self.active_line >= 0 else 1
        font = item.font()
        size = int(self.engine._prefs.get("font_size") or 24)
        font.setPixelSize(max(16, round(size * 1.17)))
        font.setBold(distance == 0)
        item.setFont(font)
        color = QColor(Tokens.TEXT_PRIMARY if distance == 0 else Tokens.TEXT_SECONDARY)
        if distance > 1:
            color.setAlpha(206)
        item.setForeground(QBrush(color))

    def _restyle(self):
        if self.isHidden():
            return
        if self.engine is not None:
            self._configure_stage()
        if self.motion_reduced():
            self._finish_cover_fade()
            if self._cover_wait.isActive():
                self._cover_wait.stop()
                self._clear_wash()
        for row in range(self.lines.count()):
            self._style(row)
        if self._follow:
            self.highlight(self.active_line, animate=False)

    def _pause_follow(self, *_):
        self._follow = False
        self._scroll.stop()

    def _resume_follow(self):
        self._follow = True
        self.highlight(self.active_line, animate=False)

    def sync_transport(self):
        if not self.isVisible() or self.engine is None:
            return
        self.stage.sync()
        state = self._scroll.state()
        if state == QAbstractAnimation.Running and not self.engine.is_playing():
            self._scroll.pause()
        elif state == QAbstractAnimation.Paused and self.engine.is_playing():
            self._scroll.resume()

    def remember_motion_preferences(self, update):
        if "reduce_motion" in update:
            self._initial_reduced = update["reduce_motion"] is True
        if "stage_low_power" in update:
            self._initial_low_power = update["stage_low_power"] is True

    def navigation_motion_reduced(self):
        low = (self._initial_low_power if self.engine is None
               else self.engine._prefs.get("stage_low_power") is True)
        return low or self.motion_reduced()

    def motion_reduced(self):
        local = self._initial_reduced if self.engine is None else self.engine._prefs.get("reduce_motion") is True
        return local or reduced_motion_enabled()

    def _set_cover_key(self, key):
        if key == self._cover_key:
            return
        self._cover_key = key
        self._cover_wait.stop()
        self._art_source = QPixmap()
        self._render_art()
        if key and self._loader is None:
            self._loader = _get_cover_loader()
            self._loader.loaded.connect(self._cover_loaded)
        cover = self._loader.get(key) if key else None
        if cover is not None and not cover.isNull():
            self._apply_cover(cover)
            return
        # The preceding mood may hold briefly while the correct cover arrives.
        # Its identifying artwork is cleared immediately.
        if key and not self._wash.isNull() and not self.motion_reduced():
            self._cover_wait.start()
        else:
            self._clear_wash()
        if key:
            self._loader.request(key)

    def _cover_loaded(self, key):
        if key != self._cover_key:
            return
        self._cover_wait.stop()
        cover = self._loader.get(key)
        if cover is not None and not cover.isNull():
            self._apply_cover(cover)
        else:
            self._clear_wash()

    def _apply_cover(self, cover):
        if cover.cacheKey() == self._art_source.cacheKey():
            return
        self._art_source = cover
        self.stage.set_artwork(cover)
        self._render_art()
        # Downsample once on delivery. The animation blends two small textures.
        tiny = cover.scaled(14, 14, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._transition_wash(tiny.scaled(96, 96, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))

    def _render_art(self):
        if self._art_source.isNull() or self.art.width() <= 0:
            self.art.setText("♪")
            return
        ratio = self.art.devicePixelRatioF()
        edge = round(self.art.width() * ratio)
        cover = self._art_source.scaled(edge, edge, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        image = QPixmap(edge, edge)
        image.fill(Qt.transparent)
        with QPainter(image) as painter:
            painter.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            radius = Tokens.WINDOW_RADIUS * ratio
            path.addRoundedRect(QRectF(0, 0, edge, edge), radius, radius)
            painter.setClipPath(path)
            painter.drawPixmap((edge - cover.width()) // 2, (edge - cover.height()) // 2, cover)
        image.setDevicePixelRatio(ratio)
        self.art.setPixmap(image)

    def _clear_wash(self):
        self.stage.set_artwork(QPixmap())
        self._transition_wash(QPixmap())

    def _transition_wash(self, wash):
        if wash.isNull() and self._wash.isNull():
            return
        previous = self._wash
        animate = self.isVisible() and not self.motion_reduced()
        if animate and self._cover_fade.state() == QAbstractAnimation.Running:
            # A third cover starts from the frame actually on screen.
            previous = QPixmap(96, 96)
            previous.fill(Qt.transparent)
            with QPainter(previous) as painter:
                self._draw_wash(painter, previous.rect())
        self._cover_fade.stop()
        self._previous_wash = previous if animate else QPixmap()
        self._wash = wash
        self._set_cover_mix(0.0 if animate else 1.0)
        if animate:
            self._cover_fade.start()

    def _set_cover_mix(self, value):
        self._cover_mix = value
        self.stage.blend_cover(value)
        self.update()

    def _finish_cover_fade(self):
        self._cover_fade.stop()
        self._previous_wash = QPixmap()
        self._set_cover_mix(1.0)

    def _draw_wash(self, painter, rect):
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._cover_mix < 1 and not self._previous_wash.isNull():
            painter.setOpacity(1 - self._cover_mix if self._wash.isNull() else 1.0)
            painter.drawPixmap(rect, self._previous_wash)
        if not self._wash.isNull():
            painter.setOpacity(self._cover_mix)
            painter.drawPixmap(rect, self._wash)
        painter.restore()

    def _paint_backdrop(self, painter):
        rect = self.rect()
        painter.fillRect(rect, QColor(Tokens.BG_BASE))
        self._draw_wash(painter, rect)
        # Leave the album side warmer while keeping the reading surface quiet.
        veil = QLinearGradient(0, 0, self.width(), self.height() * 0.65)
        for position, color, alpha in ((0.0, Tokens.BG_BASE, 136), (0.48, Tokens.BG_BASE, 209),
                                       (1.0, Tokens.BG_DEEPEST, 248)):
            tint = QColor(color)
            tint.setAlpha(alpha)
            veil.setColorAt(position, tint)
        painter.fillRect(rect, veil)
        light = QRadialGradient(self.width() * 0.19, self.height() * 0.36, max(1, self.width() * 0.47))
        glow = QColor(Tokens.ACCENT)
        glow.setAlpha(24)
        light.setColorAt(0, glow)
        glow.setAlpha(0)
        light.setColorAt(1, glow)
        painter.fillRect(rect, light)

    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        width, height = round(self.width() * ratio), round(self.height() * ratio)
        with QPainter(self) as painter:
            if self._cover_mix < 1.0:
                self._paint_backdrop(painter)
                return
            if width * height > self.MAX_BACKDROP_PIXELS:
                self._backdrop = QPixmap()
                self._backdrop_key = None
                self._paint_backdrop(painter)
                return
            key = (width, height, ratio, self._wash.cacheKey(), Tokens.BG_BASE, Tokens.BG_DEEPEST, Tokens.ACCENT)
            if key != self._backdrop_key:
                self._backdrop_key = key
                self._backdrop = QPixmap(width, height)
                self._backdrop.setDevicePixelRatio(ratio)
                with QPainter(self._backdrop) as composition:
                    self._paint_backdrop(composition)
            painter.drawPixmap(0, 0, self._backdrop)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width, height = event.size().width(), event.size().height()
        compact = width < 690
        self.body.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
        self.identity.setMinimumWidth(0)
        self.identity.setMaximumWidth(16777215 if compact else 228)
        extent = min(208, width // 4, max(112, height // 2))
        if self.art.width() != extent:
            self.art.setFixedSize(extent, extent)
            self._render_art()
        self.art.setVisible(not compact and height >= 340)

    def showEvent(self, event):
        super().showEvent(event)
        if self.engine is not None:
            self.refresh()

    def hideEvent(self, event):
        self._scroll.stop()
        if self._cover_wait.isActive():
            self._cover_wait.stop()
            self._clear_wash()
        self._finish_cover_fade()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.lines.viewport() and event.type() == QEvent.Wheel:
            self._pause_follow()
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange and hasattr(self, "_cover_fade"):
            self._restyle()
            self.update()
