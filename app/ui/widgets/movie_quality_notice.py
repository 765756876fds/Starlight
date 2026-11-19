"""Nonmodal presentation of M2.1 evidence; no playback or network operations."""
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class MovieQualityNotice(QFrame):
    view_candidates_requested = Signal()
    switch_requested = Signal()

    def __init__(self, controller, parent=None, clock=time.monotonic):
        super().__init__(parent)
        self._controller, self._clock = controller, clock
        self._session = None
        self._switch_offer = False
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
        self.view_button = QPushButton("查看影片候选")
        self.close_button = QPushButton("关闭")
        for button in (self.view_button, self.close_button):
            button.setFocusPolicy(Qt.NoFocus)
            layout.addWidget(button)
        self.view_button.clicked.connect(self._view)
        self.close_button.clicked.connect(self.dismiss)
        self.hide()

    def show_switch_offer(self, *, restart=False):
        self._session = None
        self._switch_offer = True
        self.message.setText("备用线路可能可用，换源可能从头播放。" if restart else
                             "备用来源已核对内容及读取速度。是否换源？")
        self.view_button.setText("换源")
        self.close_button.setText("继续观看")
        self.show()

    def reset(self):
        self._session = None
        self._switch_offer = False
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

    def poll(self, *, presentation_allowed=True, no_verified_candidate=False):
        """Called by the existing playback window timer on the GUI thread."""
        if self._switch_offer:
            self.reset()
        self.view_button.setText("查看影片候选")
        self.close_button.setText("关闭")
        try:
            report = self._current() if presentation_allowed else None
            now = self._clock()
            if report is None or report.state in ("UNKNOWN", "EXCLUDED"):
                self.reset()
                return
            message = (
                "没有通过验证的备用线路，可查看影片候选。" if no_verified_candidate else
                "当前播放正在等待缓存，其他片源质量尚未验证。" if report.state == "BUFFERING" else
                "刚才播放出现缓冲，其他片源质量尚未验证。"
            )
            if self._session is not None:
                if report.session != self._session or now >= self._expires:
                    self.reset()
                else:
                    self.message.setText(message)
            event = report.session, report.observed_at
            eligible = report.sustained_transport_wait if no_verified_candidate else report.advisory_eligible
            if (report.state == "BUFFERING" and eligible
                    and event != self._last_event and now >= self._cooldown_until):
                self._last_event = event
                self._session = report.session
                self._cooldown_until, self._expires = now + 120, now + 15
                self.message.setText(message)
                self.show()  # Child strip, no raise/activate/modal window.
        except Exception:
            self.reset()

    def _view(self):
        if self._switch_offer and self.isVisible():
            self.switch_requested.emit()
            return
        try:
            report = self._current()
            if (self.isVisible() and report is not None and report.session == self._session
                    and report.state not in ("UNKNOWN", "EXCLUDED")
                    and self._clock() < self._expires):
                self.view_candidates_requested.emit()
        except Exception:
            self.reset()
