"""Nonmodal presentation of M2.1 evidence; no playback or network operations."""
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class MovieQualityNotice(QFrame):
    view_sources_requested = Signal()

    def __init__(self, controller, parent=None, clock=time.monotonic):
        super().__init__(parent)
        self._controller, self._clock = controller, clock
        self._session = None
        self._last_event = None
        self._cooldown_until = self._expires = 0.
        self.setProperty("class", "card")
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message, 1)
        self.view_button = QPushButton("查看其他片源")
        self.close_button = QPushButton("关闭")
        for button in (self.view_button, self.close_button):
            button.setFocusPolicy(Qt.NoFocus)
            layout.addWidget(button)
        self.view_button.clicked.connect(self._view)
        self.close_button.clicked.connect(self.dismiss)
        self.hide()

    def reset(self):
        self._session = None
        self.hide()

    def dismiss(self):
        self._cooldown_until = self._clock() + 120
        self.reset()

    def _current(self):
        enabled = getattr(self._controller, "source_quality_trial_enabled", lambda: False)
        if enabled() is not True:
            return None
        snapshot = self._controller.source_quality_snapshot()
        report = snapshot.get("current") if snapshot else None
        if report is None or not 0 <= self._clock() - report.observed_at <= 5:
            return None
        return report

    def poll(self, *, presentation_allowed=True):
        """Called by the EXISTING playback window timer on the GUI thread."""
        try:
            report = self._current() if presentation_allowed else None
            now = self._clock()
            if report is None or report.state in ("UNKNOWN", "EXCLUDED"):
                self.reset()
                return
            if self._session is not None:
                if report.session != self._session or now >= self._expires:
                    self.reset()
                else:
                    self.message.setText(
                        "当前播放正在等待缓存，其他片源质量尚未验证。"
                        if report.state == "BUFFERING" else
                        "刚才播放出现缓冲，其他片源质量尚未验证。")
            event = report.session, report.observed_at
            if (report.state == "BUFFERING" and report.advisory_eligible
                    and event != self._last_event and now >= self._cooldown_until):
                self._last_event = event
                self._session = report.session
                self._cooldown_until, self._expires = now + 120, now + 15
                self.message.setText("当前播放正在等待缓存，其他片源质量尚未验证。")
                self.show()  # Child strip, no raise/activate/modal window.
        except Exception:
            self.reset()

    def _view(self):
        try:
            report = self._current()
            if (self.isVisible() and report is not None and report.session == self._session
                    and report.state not in ("UNKNOWN", "EXCLUDED")
                    and self._clock() < self._expires):
                self.view_sources_requested.emit()
        except Exception:
            self.reset()
