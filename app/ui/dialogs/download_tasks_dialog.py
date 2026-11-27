"""Download list backed by stable task snapshots."""
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QVBoxLayout, QWidget,
)

from ..theme.tokens import Tokens
from ..widgets.icon_utils import lucide_icon


DOWNLOAD_MESSAGES = {
    "queued": "等待下载", "resolving": "正在查找音频", "probing": "正在检查音频",
    "downloading": "正在下载", "pausing": "正在暂停", "paused": "已暂停",
    "retrying": "等待重试", "validating": "正在校验音频", "saving": "正在保存",
    "completed": "下载完成", "failed": "暂时无法下载，可重试",
    "cancelling": "正在取消", "cancelled": "已取消",
}
PAUSABLE = frozenset(("queued", "resolving", "probing", "downloading", "retrying", "validating"))
CANCELLABLE = PAUSABLE | {"paused"}
TERMINAL = frozenset(("completed", "failed", "cancelled"))


def _size(value):
    if value >= 1048576:
        return f"{value / 1048576:.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{int(value)} B"


def _byte_count(value):
    return value if type(value) is int and 0 <= value < 2**63 else None


def download_label(task, *, compact=False):
    state = task["state"]
    message = task.get("message")
    if not isinstance(message, str) or not message:
        message = DOWNLOAD_MESSAGES.get(state, "下载任务")
    if compact and state in ("fail", "failed"):
        message = message.split("，", 1)[0]
    details = task.get("details") or task
    if state == "completed":
        if details.get("fallback") is True:
            message += "，已使用较低的可用品质"
        missing = [name for field, name in (("lyrics", "歌词"), ("cover", "封面"), ("metadata", "歌曲信息"))
                   if details.get(field) == "unavailable"]
        if missing:
            message += "，" + "、".join(missing) + "暂缺"
        if details.get("indexed") is False:
            message += "，曲库尚未更新"
    parts = [message]
    if isinstance(details.get("format"), str) and details["format"]:
        parts.append(details["format"])
    quality = details.get("quality")
    if isinstance(quality, str) and quality:
        bits = _byte_count(details.get("bit_depth"))
        parts.append(f"{bits} bit {quality}" if bits and quality == "无损" else quality)
    rate = _byte_count(details.get("sample_rate"))
    if not compact and rate:
        parts.append(f"{rate / 1000:g} kHz")
    written, total = (_byte_count(task.get(key)) for key in ("bytes_written", "total_bytes"))
    final_size = _byte_count(details.get("size_bytes"))
    if written is not None and state not in ("completed", "done") and (written or total):
        progress = _size(written)
        if total and 0 <= written <= total:
            progress += f" / {_size(total)} ({written * 100 // total}%)"
        parts.append(progress)
    elif final_size is not None or total is not None:
        parts.append(_size(final_size if final_size is not None else total))
    if state == "downloading":
        speed, eta = task.get("speed_bps"), task.get("eta_seconds")
        if type(speed) in (int, float) and 0 < speed < 2**63:
            parts.append(f"{_size(speed)}/s" if speed >= 1 else "<1 B/s")
        if type(eta) in (int, float) and 0 <= eta < 2**63:
            minutes, seconds = divmod(round(eta), 60)
            parts.append(f"剩约 {minutes} 分 {seconds} 秒" if minutes else f"剩约 {seconds} 秒")
    return " · ".join(parts)


class DownloadTasksPane(QWidget):
    retry_requested = Signal(str)
    operation_requested = Signal(str, object)
    close_requested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*([Tokens.SPACING_MD] * 4))
        layout.setSpacing(Tokens.SPACING_SM)
        header = QHBoxLayout()
        title = QLabel("下载任务")
        title.setProperty("class", "title")
        header.addWidget(title, 1)
        self.btn_close = QPushButton()
        self.btn_close.setFixedSize(32, 30)
        self.btn_close.setIcon(lucide_icon("x", 14, Tokens.TEXT_SECONDARY))
        self.btn_close.setAccessibleName("收起下载任务")
        self.btn_close.setToolTip("收起下载任务（Esc）")
        self.btn_close.clicked.connect(self.close_requested.emit)
        header.addWidget(self.btn_close)
        layout.addLayout(header)
        self.tasks = {}
        self.rows = {}
        self.items = QListWidget()
        self.items.setWordWrap(True)
        self.items.setUniformItemSizes(True)
        self.items.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.items, 1)
        self.hint = QLabel("暂无下载任务")
        self.hint.setTextFormat(Qt.PlainText)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        self.pause = QPushButton("暂停")
        self.resume = QPushButton("继续")
        self.retry = QPushButton("重试")
        self.open_folder = QPushButton("打开文件夹")
        self.more = QPushButton("更多")
        self.more.setAccessibleName("更多下载任务操作")
        menu = QMenu(self.more)
        self.cancel_action = menu.addAction("取消下载")
        self.remove_action = menu.addAction("移除任务记录")
        self.remove_action.setToolTip("已保存的音频仍保留在下载目录")
        menu.setToolTipsVisible(True)
        self.more.setMenu(menu)
        self.pause.clicked.connect(lambda: self._operate("pause", PAUSABLE))
        self.resume.clicked.connect(lambda: self._operate("resume", {"paused"}))
        self.retry.clicked.connect(self._retry)
        self.cancel_action.triggered.connect(lambda: self._operate("cancel", CANCELLABLE))
        self.remove_action.triggered.connect(lambda: self._operate("remove", TERMINAL | {"paused"}))
        self.open_folder.clicked.connect(self._open_folder)
        actions.addStretch()
        for button in (self.pause, self.resume, self.retry, self.open_folder, self.more):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.items.itemSelectionChanged.connect(self._sync_action)
        self.items.currentItemChanged.connect(self._sync_action)
        self._sync_action()

    def update_task(self, key, task):
        self.tasks[key] = task
        item = self.rows.get(key)
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, key)
            item.setSizeHint(QSize(1, 4 * self.items.fontMetrics().lineSpacing() + Tokens.SPACING_SM))
            self.items.addItem(item)
            self.rows[key] = item
        song = task["song"]
        identity = " · ".join(str(song[k]) for k in ("title", "artist", "album") if song.get(k))
        item.setText(f"{identity}\n{download_label(task, compact=True)}")
        item.setToolTip(f"{identity}\n{download_label(task)}")
        if self.items.currentItem() is None:
            self.items.setCurrentItem(item)
        self._sync_action()

    def forget(self, key):
        self.tasks.pop(key, None)
        item = self.rows.pop(key, None)
        if item is not None:
            self.items.takeItem(self.items.row(item))
        self._sync_action()

    def _selected_keys(self):
        items = self.items.selectedItems()
        if not items and self.items.currentItem() is not None:
            items = [self.items.currentItem()]
        return tuple(item.data(Qt.UserRole) for item in items)

    def _eligible(self, states):
        return tuple(key for key in self._selected_keys() if self.tasks.get(key, {}).get("state") in states)

    def _operate(self, operation, states):
        keys = self._eligible(states)
        if keys:
            self.operation_requested.emit(operation, keys)

    def _retry(self):
        self._operate("retry", {"failed"})
        for key in self._eligible({"fail"}):
            self.retry_requested.emit(key)

    def _sync_action(self, *_):
        keys = self._selected_keys()
        task = self.tasks.get(keys[0], {}) if len(keys) == 1 else {}
        failed = task.get("state") in ("fail", "failed")
        done = bool(task.get("path") and task.get("state") in ("done", "completed"))
        hint = (task.get("message") or DOWNLOAD_MESSAGES["failed"]) if failed else (
            f"已选 {len(keys)} 项 · 操作应用于其中可操作的任务" if len(keys) > 1 else
            "" if self.tasks else "暂无下载任务")
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        for button, enabled in (
            (self.pause, bool(self._eligible(PAUSABLE))),
            (self.resume, bool(self._eligible({"paused"}))),
            (self.retry, bool(self._eligible({"fail", "failed"}))),
            (self.open_folder, done),
        ):
            button.setEnabled(enabled)
            button.setVisible(enabled)
        can_cancel = bool(self._eligible(CANCELLABLE))
        can_remove = bool(self._eligible(TERMINAL | {"paused"}))
        self.cancel_action.setEnabled(can_cancel)
        self.remove_action.setEnabled(can_remove)
        self.more.setVisible(can_cancel or can_remove)

    def _open_folder(self):
        keys = self._selected_keys()
        task = self.tasks.get(keys[0], {}) if len(keys) == 1 else {}
        if task.get("state") not in ("done", "completed") or not task.get("path"):
            return
        path = Path(task["path"])
        self.hint.show()
        if not path.is_file():
            self.hint.setText("文件已移动或删除")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.absolute().parent))):
            self.hint.setText("未能打开文件夹")
        else:
            self.hint.hide()
