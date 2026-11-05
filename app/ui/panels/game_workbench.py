"""A read-only game overview around existing player data and cooldown state."""
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QListWidget, QListWidgetItem, QSplitter)

from ...core.notifications import notify

logger = logging.getLogger(__name__)


class GameWorkbench(QWidget):
    def __init__(self, panel, cooldown=None, lcu=None, parent=None):
        super().__init__(parent)
        self.panel, self.cooldown, self.lcu = panel, cooldown, lcu
        self._read_error = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.connection = QLabel("等待游戏客户端")
        self.connection.setProperty("class", "title")
        self.game_status = QLabel("尚未同步游戏时间")
        self.game_status.setTextFormat(Qt.PlainText)
        head.addWidget(self.connection)
        head.addWidget(self.game_status, 1)
        root.addLayout(head)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(panel)
        timers = QWidget()
        layout = QVBoxLayout(timers)
        title = QLabel("技能计时")
        title.setProperty("class", "title")
        layout.addWidget(title)
        self.reminders = QListWidget()
        self.reminders.setWordWrap(True)
        layout.addWidget(self.reminders, 1)
        self.empty = QLabel("尚无计时。可说“石头人闪现用了”。")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self.cancel_button = QPushButton("取消所选计时")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.reminders.currentItemChanged.connect(lambda item: self.cancel_button.setEnabled(item is not None))
        layout.addWidget(self.cancel_button)
        hint = QLabel("语音示例：\n石头人闪现用了\n取消石头人计时\n查询技能计时\n\n按游戏内时间计时；未同步时不会猜测剩余时间。")
        hint.setWordWrap(True)
        hint.setProperty("class", "secondary")
        layout.addWidget(hint)
        self.splitter.addWidget(timers)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        root.addWidget(self.splitter, 1)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        self.splitter.setOrientation(Qt.Vertical if event.size().width() < 780 else Qt.Horizontal)
        super().resizeEvent(event)

    def refresh(self):
        connected = getattr(self.lcu, "connected", False) is True
        self.connection.setText("游戏客户端已连接" if connected else "游戏客户端未连接")
        self.connection.setProperty("class", "status-success" if connected else "secondary")
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)
        # Reuse the already refreshed overview. Do not perform another LCU request.
        title = getattr(self.panel, "lbl_title", None)
        self.game_status.setText(title.text() if title is not None else "等待对局信息")
        current = self.reminders.currentItem()
        selected = current.data(Qt.UserRole) if current else None
        try:
            rows = self.cooldown.get_active_reminders() if self.cooldown is not None else []
            if not isinstance(rows, list):
                rows = []
            rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("remaining"), (int, float)) and r["remaining"] > 0]
            self._read_error = False
        except Exception:
            if not self._read_error:
                logger.exception("game_workbench_cooldown_read_failed")
                notify("技能计时暂不可读取", "请稍后重试；原有计时和提醒没有被修改。", source="lol", status="warning")
            self._read_error = True
            return
        self.reminders.clear()
        for row in rows:
            side = {"enemy": "敌方", "ally": "我方", "self": "自己"}.get(row.get("side"), "")
            item = QListWidgetItem(f"{side} {row.get('champion_name') or '未指定英雄'} · {row.get('skill', '')}\n剩余 {int(row['remaining'])} 秒"
                                  + (" · 已提醒" if row.get("status") == "reminded" else ""))
            item.setData(Qt.UserRole, row.get("id"))
            self.reminders.addItem(item)
            if row.get("id") == selected:
                self.reminders.setCurrentItem(item)
        self.empty.setVisible(not rows)
        self.cancel_button.setEnabled(self.reminders.currentItem() is not None)

    def _cancel_selected(self):
        item = self.reminders.currentItem()
        if item is None or self.cooldown is None:
            return
        try:
            result = self.cooldown.cancel(item.data(Qt.UserRole))
        except Exception:
            logger.exception("game_workbench_cancel_failed")
            result = None
        notify("技能计时已取消" if result is True else "未能取消技能计时",
               "所选计时已移除" if result is True else "该计时可能已结束，请查看最新列表后重试。",
               source="lol", status="success" if result is True else "warning")
        self.refresh()
