"""Session view of the existing downloader's callbacks."""
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from ..theme.tokens import Tokens
from ..widgets.icon_utils import lucide_icon


def download_label(task, *, compact=False):
    message = task["message"]
    if compact and task["state"] == "fail":
        message = message.split("，", 1)[0]
    parts = [message]
    if task.get("format"):
        parts.append(task["format"])
    if task.get("quality"):
        parts.append(task["quality"])
    if task.get("size_bytes") is not None:
        size = task["size_bytes"]
        parts.append(f"{size / 1048576:.1f} MiB" if size >= 1048576 else f"{size / 1024:.1f} KiB")
    return " · ".join(parts)


class DownloadTasksPane(QWidget):
    retry_requested = Signal(str)
    close_requested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Tokens.SPACING_SM, Tokens.SPACING_SM, 0, 0)
        layout.setSpacing(Tokens.SPACING_SM)
        header = QHBoxLayout()
        title = QLabel("下载任务")
        title.setProperty("class", "title")
        header.addWidget(title, 1)
        self.btn_close = QPushButton()
        self.btn_close.setFixedSize(32, 30)
        self.btn_close.setIcon(lucide_icon("x", 14, Tokens.TEXT_SECONDARY))
        self.btn_close.setAccessibleName("收起下载任务")
        self.btn_close.setToolTip("收起下载任务")
        self.btn_close.clicked.connect(self.close_requested.emit)
        header.addWidget(self.btn_close)
        layout.addLayout(header)
        self.tasks = {}
        self.rows = {}
        self.items = QListWidget()
        self.items.setWordWrap(True)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.items, 1)
        self.hint = QLabel("暂无下载任务")
        self.hint.setTextFormat(Qt.PlainText)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        self.retry = QPushButton("重试")
        self.open_folder = QPushButton("打开文件夹")
        self.retry.clicked.connect(lambda: self.retry_requested.emit(self._selected_key()))
        self.open_folder.clicked.connect(self._open_folder)
        actions.addStretch()
        actions.addWidget(self.retry)
        actions.addWidget(self.open_folder)
        layout.addLayout(actions)
        self.items.currentItemChanged.connect(self._sync_action)
        self._sync_action()

    def update_task(self, key, task):
        self.tasks[key] = task
        item = self.rows.get(key)
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, key)
            self.items.insertItem(0, item)
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

    def _selected_key(self):
        item = self.items.currentItem()
        return item.data(Qt.UserRole) if item is not None else ""

    def _sync_action(self, *_):
        task = self.tasks.get(self._selected_key(), {})
        failed = task.get("state") == "fail"
        done = bool(task.get("path") and task.get("state") == "done")
        hint = task["message"] if failed else "" if self.tasks else "暂无下载任务"
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.retry.setEnabled(failed)
        self.retry.setVisible(failed)
        self.open_folder.setEnabled(done)
        self.open_folder.setVisible(done)

    def _open_folder(self):
        task = self.tasks.get(self._selected_key(), {})
        if task.get("state") != "done" or not task.get("path"):
            return
        path = Path(task["path"])
        self.hint.show()
        if not path.is_file():
            self.hint.setText("文件已移动或删除")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.absolute().parent))):
            self.hint.setText("未能打开文件夹")
        else:
            self.hint.hide()
