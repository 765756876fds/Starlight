"""Optional constraints for verified representations of a selected recording."""
from threading import Event

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton

from ..settings_store import load_download_prefs, save_download_prefs
from ..widgets.glass_dialog import GlassDialog


class DownloadOptionsDialog(GlassDialog):
    verified_ready = Signal(object)

    def __init__(self, parent=None, *, song=None, verified=(), probe=None):
        super().__init__("自定义下载", parent)
        self.setMinimumWidth(360)
        self._prefs = load_download_prefs()
        self._verified = tuple(verified)
        self._probe = probe
        self._probe_cancel = Event()
        self.finished.connect(lambda _: self._probe_cancel.set())
        self.verified_ready.connect(self._update_formats)
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
        self._update_formats(self._verified)

        self.info = QLabel("还未验证这首歌的音质和格式，将自动选择可用的原始音频。")
        self.info.setWordWrap(True)
        self.info.setVisible(not self._verified)
        self.body_layout.addWidget(self.info)

        self.choices = {}
        for key, label in (("lyrics", "保存匹配歌词"), ("cover", "保存封面"), ("metadata", "写入歌曲信息")):
            checkbox = QCheckBox(label)
            checkbox.setChecked(self._prefs[key])
            self.choices[key] = checkbox
            self.body_layout.addWidget(checkbox)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        if probe is not None:
            self.check_options = QPushButton("检查其他音质和格式")
            self.check_options.setToolTip("检查会读取候选音频，结果只在本次运行中保留")
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

    def _check_options(self):
        self.status.setText("正在检查可用音频…")
        self.check_options.setEnabled(False)

        def ready(choices):
            try:
                self.verified_ready.emit(choices)
            except RuntimeError:
                pass

        if not self._probe(ready, self._probe_cancel.is_set):
            self.status.setText("暂时无法检查，可以直接下载")
            self.check_options.setEnabled(True)

    def _update_formats(self, verified):
        if self._probe_cancel.is_set():
            return
        self._verified = tuple(verified)
        previous = self.format.currentData() or self._prefs.get("format")
        self.format.blockSignals(True)
        self.format.clear()
        self.format.addItem("原始格式", None)
        for extension in sorted({ext for _, ext, _ in self._verified}):
            self.format.addItem(extension[1:].upper(), extension)
        self.format.setCurrentIndex(max(0, self.format.findData(previous)))
        self.format.blockSignals(False)
        self._set_qualities()
        if hasattr(self, "status"):
            self.info.setVisible(not verified)
            self.status.setText("可选音质和格式已更新" if verified else "暂未验证到可用音频")
            if hasattr(self, "check_options"):
                self.check_options.setEnabled(True)

    def _set_qualities(self):
        self.quality.clear()
        self.quality.addItem("最高可用音质", None)
        extension = self.format.currentData()
        offered = set()
        for tier, fmt, quality in self._verified:
            if (extension and fmt != extension) or quality in offered or quality == "音质未标注":
                continue
            offered.add(quality)
            self.quality.addItem(quality, [tier, quality, fmt])
        current = [self._prefs.get("quality"), self._prefs.get("verified_quality"), self._prefs.get("format")]
        self.quality.setCurrentIndex(max(0, self.quality.findData(current)))

    def options(self):
        options = {key: check.isChecked() for key, check in self.choices.items()}
        tier = self.quality.currentData()
        options["quality"] = tier[0] if tier else "best"
        if tier:
            options["verified_quality"] = tier[1]
            options["format"] = tier[2]
        if self.format.currentData():
            options["format"] = self.format.currentData()
        return options

    def _accept(self):
        options = self.options()
        prefs = dict(options)
        prior_format = self._prefs.get("format")
        prior_quality = [self._prefs.get("quality"), self._prefs.get("verified_quality"), prior_format]
        if not options.get("format") and prior_format and self.format.findData(prior_format) < 0:
            prefs["format"] = prior_format
        if options["quality"] == "best" and self.quality.findData(prior_quality) < 0:
            for key in ("quality", "verified_quality"):
                if key in self._prefs:
                    prefs[key] = self._prefs[key]
        if save_download_prefs(prefs):
            self.accept()
        else:
            self.status.setText("偏好未能保存，请重试。")
