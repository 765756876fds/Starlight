from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QMenu, QPushButton

from ..theme.tokens import Tokens

from ...music.playback_quality import PREFERENCES, PlaybackQuality
from ..settings_store import load_ui_prefs, save_ui_prefs
from .latest_task import LatestTask


class _PlaybackMenu(QMenu):
    def showEvent(self, event):
        super().showEvent(event)
        button = self.parentWidget()
        screen = button.screen().availableGeometry()
        top = button.mapToGlobal(QPoint(0, 0))
        y = top.y() - self.height() - 4
        if y < screen.top():
            y = min(top.y() + button.height(), screen.bottom() + 1 - self.height())
        x = min(top.x(), screen.right() + 1 - self.width())
        self.move(max(screen.left(), x), max(screen.top(), y))


class PlaybackQualityButton(QPushButton):
    def __init__(self, music, parent):
        super().__init__("播放音质", parent)
        self.setAccessibleName("播放音质")
        self.setToolTip("查看实际播放音质与切换选项")
        self.setFixedHeight(22)
        self.setStyleSheet(
            f"QPushButton {{ padding: 0 16px 0 6px; font-size:11px; color:{Tokens.TEXT_SECONDARY};"
            f" border:1px solid {Tokens.BORDER}; border-radius:5px; background:transparent; }}"
            f"QPushButton:hover, QPushButton:focus {{ color:{Tokens.TEXT_PRIMARY}; border-color:{Tokens.ACCENT}; }}"
        )
        self.backend = getattr(music, "playback_quality", None)
        if not isinstance(self.backend, PlaybackQuality):
            self.backend = None
        self.probe = LatestTask(self)
        self.switch = LatestTask(self)
        self.probe.completed.connect(self._probed)
        self.switch.completed.connect(self._switched)
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(lambda: self.refresh(force=True))
        self.key = None
        self.attempts = 0
        self.facts = None
        self.setMenu(_PlaybackMenu(self))
        self.menu().aboutToShow.connect(self._opened)
        self.group = QActionGroup(self)
        self.actions_by_key = {}
        pref = load_ui_prefs().get("playback_quality", "auto")
        self.pref = pref if pref in PREFERENCES else "auto"
        if self.backend is not None:
            self.backend.preference = self.pref
        self.scope = self.menu().addSection("播放音质")
        for key, label in PREFERENCES.items():
            action = self.menu().addAction(label)
            action.setCheckable(True)
            action.setData(key)
            self.group.addAction(action)
            self.actions_by_key[key] = action
        self.actions_by_key[self.pref].setChecked(True)
        self.group.triggered.connect(lambda action: self.select(action.data()))
        self.menu().addSeparator()
        self.metadata = [self.menu().addAction("") for _ in range(4)]
        for action in self.metadata:
            action.setEnabled(False)
        self._describe()

    def _identity(self):
        if self.backend is None:
            return None
        song = self.backend.owner.get_current_song()
        return (id(song), self.backend.revision) if song else None

    def refresh(self, *, force=False):
        key = self._identity()
        if key == self.key and not force:
            return
        if key != self.key:
            self.retry.stop()
            self.facts = None
            self.attempts = 0
            self.key = key
            self.setText("播放音质")
            self._describe()
        if key and self.isVisible():
            self.attempts += 1
            self.probe.submit(key, self.backend.inspect)

    def _opened(self):
        self._describe()
        self.refresh(force=True)

    def _probed(self, key, value, error):
        if key != self._identity() or not self.isVisible():
            return
        if not value:
            if self.attempts < 4:
                self.retry.start(800)
            return
        self.facts = value
        codec = value.get("codec") or ""
        if codec.startswith("PCM_"):
            codec = "PCM"
        self.setText(" · ".join(part for part in (codec, self.rate(value.get("sample_rate"))) if part)
                     or "播放音质")
        self._describe()

    @staticmethod
    def rate(value):
        return f"{value/1000:g} kHz" if value else ""

    def _describe(self):
        data = self.facts or {}
        f = lambda key: data.get(key) or "未知"
        song = self.backend.owner.get_current_song() if self.backend else None
        online = bool(song and song.get("source") == "online")
        self.scope.setText("当前歌曲与后续在线播放" if online else "后续在线播放偏好")
        bits = f"{data['bits']} bit" if data.get("bits") else "位深未知"
        bitrate = f"{data['bitrate']/1000:.0f} kb/s" if data.get("bitrate") else "码率未知"
        channels = f"{data['channels']} 声道" if data.get("channels") else "声道未知"
        lines = [f"当前编码  {f('codec')} · {self.rate(data.get('sample_rate')) or '采样率未知'}",
                 f"{bits} · {bitrate} · {channels}",
                 f"设备输出  {self.rate(data.get('output_rate')) or '未知'} · {f('output_format')} · {f('output_channels')} 声道",
                 f"此曲请求  {PREFERENCES.get(data.get('requested_preference'), '未知')} / {f('requested')}    来源声明  {f('declared')}" if online else "本地文件按原编码播放"]
        for action,text in zip(self.metadata,lines):
            action.setText(text)
        self.setToolTip("查看实际播放参数与切换选项")

    def select(self, key):
        if key not in PREFERENCES:
            return
        if not save_ui_prefs({"playback_quality": key}):
            self.actions_by_key[self.pref].setChecked(True)
            self.setText("偏好未保存")
            return
        self.pref = key
        if self.backend is None:
            return
        request = self.backend.reserve(key)
        if request[2] is None or request[2] is not self.backend.song:
            return
        self.setText("正在切换…")
        self.switch.submit(request[0], lambda current: self.backend.change(request, current), guarded=True)

    def _switched(self, serial, message, error):
        if self.backend is None or serial != self.backend.serial:
            return
        if error or message:
            from ...core.notifications import notify
            text = "暂时无法切换，请重试" if error else message
            notify("播放音质",text,status="warning" if error or "不可" in text or "重试" in text else "info")
        self.setText("播放音质")
        self.refresh(force=True)

    def shutdown(self):
        self.probe.cancel()
        self.switch.cancel()
        self.retry.stop()
