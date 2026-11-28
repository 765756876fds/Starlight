"""Download preferences and evidence for the selected recording's representations."""
from threading import Event

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton

from ...music.download_selection import CONTAINERS, DownloadOption
from ...music.online.search_resources import _QUALITY as _DECLARED_QUALITY
from ..settings_store import load_download_prefs, save_download_prefs
from ..widgets.glass_dialog import GlassDialog


class DownloadOptionsDialog(GlassDialog):
    verified_ready = Signal(int, object)

    def __init__(self, parent=None, *, song=None, verified=(), probe=None):
        super().__init__("自定义下载", parent)
        self.setMinimumWidth(360)
        self._prefs = load_download_prefs()
        self._verified = ()
        self._probe = probe
        self._probe_cancel = Event()
        self._probe_serial = 0
        self._checking = False
        self._selection_needs_confirmation = False
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._check_options)
        self.finished.connect(self._cancel_probe)
        self.verified_ready.connect(self._on_probe_ready)
        if song:
            identity = QLabel(" · ".join(str(song[key]) for key in ("title", "artist", "album") if song.get(key)))
            identity.setTextFormat(Qt.PlainText)
            identity.setWordWrap(True)
            self.body_layout.addWidget(identity)

        self.format = QComboBox()
        self.format.setAccessibleName("下载格式")
        self.body_layout.addWidget(self.format)

        self.quality = QComboBox()
        self.quality.setAccessibleName("下载音质")
        self.body_layout.addWidget(self.quality)
        self.format.currentIndexChanged.connect(self._set_qualities)
        self.quality.currentIndexChanged.connect(self._update_details)
        self.format.activated.connect(self._confirm_selection)
        self.quality.activated.connect(self._confirm_selection)

        self.info = QLabel("具体音质和大小待确认，可自动选择可用的原始音频。")
        self.info.setWordWrap(True)
        self.body_layout.addWidget(self.info)
        self.details = QLabel()
        self.details.setTextFormat(Qt.PlainText)
        self.details.setWordWrap(True)
        self.body_layout.addWidget(self.details)

        self.choices = {}
        for key, label in (("lyrics", "保存匹配歌词"), ("cover", "保存封面"), ("metadata", "写入歌曲信息"),
                           ("allow_fallback", "自动选择时允许较低品质")):
            checkbox = QCheckBox(label)
            checkbox.setChecked(self._prefs[key])
            self.choices[key] = checkbox
            self.body_layout.addWidget(checkbox)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        if probe is not None:
            self.check_options = QPushButton("重新检查可用资源")
            self.check_options.setToolTip("仅检查所选歌曲的候选资源，下载后仍会校验完整文件")
            self.check_options.clicked.connect(self._check_options)
            self.body_layout.addWidget(self.check_options)
        self.body_layout.addWidget(self.status)
        row = QHBoxLayout()
        self.cancel = QPushButton("取消")
        self.download = QPushButton("下载")
        self.download.setProperty("class", "primary")
        self.cancel.clicked.connect(self.reject)
        self.download.clicked.connect(self._accept)
        row.addStretch()
        row.addWidget(self.cancel)
        row.addWidget(self.download)
        self.body_layout.addLayout(row)
        self._update_formats(verified)

    def showEvent(self, event):
        super().showEvent(event)
        if self._probe is not None and not self._probe_serial and not self._probe_cancel.is_set():
            self._auto_timer.start(0)

    def hideEvent(self, event):
        self._cancel_probe()
        super().hideEvent(event)

    def _cancel_probe(self, *_):
        self._auto_timer.stop()
        self._probe_cancel.set()

    def _check_options(self):
        if self._checking or self._probe_cancel.is_set():
            return
        self._auto_timer.stop()
        self._probe_serial += 1
        serial = self._probe_serial
        self._checking = True
        self.status.setText("正在检查可用音频…")
        self.check_options.setEnabled(False)
        self._update_details()

        def ready(choices):
            if self._probe_cancel.is_set():
                return
            try:
                self.verified_ready.emit(serial, choices)
            except RuntimeError:
                pass

        if not self._probe(ready, self._probe_cancel.is_set):
            self._checking = False
            self.status.setText("暂时无法检查，可自动下载或重试")
            self.check_options.setEnabled(True)
            self._update_details()

    def _on_probe_ready(self, serial, choices):
        if self._probe_cancel.is_set() or serial != self._probe_serial:
            return
        self._checking = False
        previous_format, previous_quality = self.format.currentData(), self.quality.currentData()
        self._update_formats(choices)
        if ((previous_format is not None and previous_format != self.format.currentData())
                or (previous_quality is not None
                    and not self._same_choice(previous_quality, self.quality.currentData()))):
            self._selection_needs_confirmation = True
        self.status.setText("原选项已变化，请重新选择格式或音质" if self._selection_needs_confirmation else
                            "已更新可访问的资源" if self._verified else "暂未确认到可用资源，可自动下载或重试")
        self._update_details()
        self.check_options.setEnabled(True)

    def _confirm_selection(self, *_):
        if self._selection_needs_confirmation:
            self._selection_needs_confirmation = False
            self.status.clear()
            self._update_details()

    @staticmethod
    def _same_choice(left, right):
        if not isinstance(left, DownloadOption) or not isinstance(right, DownloadOption):
            return left == right
        return (left.resource == right.resource and
                (left.extension, left.codec, left.bit_depth, left.sample_rate, left.channels, left.bitrate) ==
                (right.extension, right.codec, right.bit_depth, right.sample_rate, right.channels, right.bitrate))

    @staticmethod
    def _fields(option):
        if isinstance(option, DownloadOption):
            return option.tier, option.extension, option.label
        return tuple(option) if isinstance(option, (list, tuple)) and len(option) == 3 else (None, None, None)

    def _update_formats(self, verified):
        previous_format = self.format.currentData() if self._verified else self._prefs.get("format")
        previous_quality = self.quality.currentData()
        allowed_formats = set(CONTAINERS.values())
        self._verified = tuple(option for option in verified if self._fields(option)[1] in allowed_formats)
        self.format.blockSignals(True)
        self.format.clear()
        self.format.addItem("原始格式", None)
        for extension in sorted({self._fields(option)[1] for option in self._verified}):
            self.format.addItem(extension[1:].upper(), extension)
        self.format.setCurrentIndex(max(0, self.format.findData(previous_format)))
        self.format.blockSignals(False)
        self._set_qualities(previous=previous_quality)
        self.info.setVisible(not self._verified)

    def _set_qualities(self, *_, previous=None):
        if previous is None:
            previous = self.quality.currentData()
        self.quality.blockSignals(True)
        self.quality.clear()
        self.quality.addItem("最高可用音质", None)
        extension = self.format.currentData()
        offered = set()
        exact_index = preferred_index = 0
        for option in self._verified:
            tier, fmt, quality = self._fields(option)
            if (extension and fmt != extension) or quality == "音质未标注":
                continue
            if isinstance(option, DownloadOption):
                key = (fmt, option.codec, option.bit_depth, option.sample_rate, option.channels, option.bitrate)
                label = f"{quality} · {fmt[1:].upper()}"
            else:
                key = (fmt, quality)
                label = quality
            if key in offered:
                continue
            offered.add(key)
            self.quality.addItem(label, option)
            index = self.quality.count() - 1
            if self._same_choice(option, previous):
                exact_index = index
            if (not preferred_index and tier == self._prefs.get("quality")
                    and fmt == self._prefs.get("format")
                    and (isinstance(option, DownloadOption) or quality == self._prefs.get("verified_quality"))):
                preferred_index = index
        self.quality.setCurrentIndex(exact_index or preferred_index)
        self.quality.blockSignals(False)
        self._update_details()

    def _update_details(self, *_):
        option = self.quality.currentData()
        self.download.setEnabled(not self._selection_needs_confirmation and (
            not self._checking or (option is None and self.format.currentData() is None)))
        fallback = self.choices["allow_fallback"]
        fallback.setEnabled(option is None)
        fallback.setToolTip("选择具体音质后，资源不可用时会提示失败，不会换成较低音质。"
                            if option is not None else "")
        if not isinstance(option, DownloadOption):
            self.details.clear()
            self.details.setVisible(False)
            return
        if option.size_bytes is None:
            size = "文件大小未知"
        else:
            total = option.size_bytes
            size = f"{total / 1048576:.1f} MiB" if total >= 1048576 else f"{total / 1024:.1f} KiB" if total >= 1024 else f"{total} B"
            size += " · " + ("来源标注" if option.size_evidence == "source_declared" else "资源返回的完整大小")
        audio = [option.codec.upper() if option.codec else "编码待确认",
                 f"{option.bitrate / 1000:g} kbps" if option.bitrate else None,
                 f"{option.sample_rate / 1000:g} kHz" if option.sample_rate else None,
                 f"{option.bit_depth} bit" if option.bit_depth else None,
                 f"{option.channels} 声道" if option.channels else None]
        declared = _DECLARED_QUALITY.get(option.declared_quality)
        status = f"来源标注 {declared} · 可访问" if declared else "可访问"
        self.details.setText(f"{size}\n{' · '.join(value for value in audio if value)}\n{status} · 下载后校验完整文件")
        self.details.setVisible(True)
        if self.isVisible() and self.height() < self.sizeHint().height():
            self.resize(self.width(), self.sizeHint().height())

    def options(self):
        options = {key: check.isChecked() for key, check in self.choices.items()}
        choice = self.quality.currentData()
        tier, fmt, quality = self._fields(choice)
        options["quality"] = tier or "best"
        if isinstance(choice, DownloadOption):
            options["representation"] = choice
            options["format"] = fmt
        elif choice:
            options["verified_quality"] = quality
            options["format"] = fmt
        if self.format.currentData():
            options["format"] = self.format.currentData()
        return options

    def _accept(self):
        if not self.download.isEnabled():
            return
        options = self.options()
        prefs = {key: value for key, value in options.items() if key != "representation"}
        prior_format = self._prefs.get("format")
        if not options.get("format") and prior_format and self.format.findData(prior_format) < 0:
            prefs["format"] = prior_format
        if self.quality.currentData() is None and not any(
            self._fields(option)[:2] == (self._prefs.get("quality"), prior_format) for option in self._verified
        ):
            for key in ("quality", "verified_quality"):
                if key in self._prefs:
                    prefs[key] = self._prefs[key]
        if save_download_prefs(prefs):
            self.accept()
        else:
            self.status.setText("偏好未能保存，请重试。")
