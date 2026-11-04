"""Queue display snapshots with guarded requests to remove non-current entries."""
from html import escape

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QDialog, QLabel, QListView, QMenu, QPushButton, QVBoxLayout

from ..theme.tokens import Tokens


class QueueListModel(QAbstractListModel):
    def __init__(self, numbered=False, parent=None):
        super().__init__(parent)
        self.rows = []
        self.current_index = None
        self.numbered = numbered

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        current = row["index"] == self.current_index
        prefix = f"{index.row() + 1}. " if self.numbered else "当前 · " if current else ""
        artist = f" — {row['artist']}" if row["artist"] else ""
        source = ("在线 · " + row["source_name"] if row["source_name"] else "在线") \
            if row["source_type"] == "online" else "本地"
        text = f"{prefix}{row['title']}{artist}\n{source}"
        if role == Qt.DisplayRole:
            return text
        if role == Qt.ToolTipRole:
            return "<qt>" + escape(text).replace("\n", "<br/>") + "</qt>"
        if role == Qt.ForegroundRole:
            return QColor(Tokens.ACCENT if current else Tokens.TEXT_PRIMARY)
        if role == Qt.FontRole and current:
            font = QFont()
            font.setBold(True)
            return font
        return None

    def update_rows(self, rows, current_index):
        if rows != self.rows:
            self.beginResetModel()
            self.rows = rows
            self.current_index = current_index
            self.endResetModel()
        elif current_index != self.current_index:
            old = self.current_index
            self.current_index = current_index
            for i, row in enumerate(self.rows):
                if row["index"] in (old, current_index):
                    self.dataChanged.emit(self.index(i), self.index(i))


class MusicQueueDialog(QDialog):
    remove_requested = Signal(int, int)  # underlying index, structural revision

    def __init__(self, parent=None):
        super().__init__(parent)
        self._revision = None
        self._protected_indices = set()
        self._editable = False
        self.setObjectName("musicQueueDialog")
        self.setWindowTitle("播放队列")
        self.setModal(False)
        self.resize(560, 600)
        self.setMinimumSize(380, 340)
        self.setStyleSheet(f"QDialog#musicQueueDialog {{ background: {Tokens.BG_BASE}; }}"
                           f"QLabel {{ color: {Tokens.TEXT_PRIMARY}; }}"
                           f"QListView {{ background: {Tokens.BG_ELEVATED}; color: {Tokens.TEXT_PRIMARY};"
                           f" border: 1px solid {Tokens.BORDER}; border-radius: {Tokens.RADIUS_SMALL}px;"
                           f" padding: {Tokens.SPACING_SM}px; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*([Tokens.SPACING_MD] * 4))
        layout.setSpacing(Tokens.SPACING_SM)
        self.mode_label = QLabel("播放模式：读取中")
        self.mode_label.setProperty("class", "secondary")
        self.current_label = QLabel("正在读取播放状态…")
        self.current_label.setProperty("class", "title")
        self.current_label.setTextFormat(Qt.PlainText)
        self.current_label.setWordWrap(True)
        layout.addWidget(self.mode_label)
        layout.addWidget(self.current_label)
        self.notice = QLabel("")
        self.notice.setProperty("class", "secondary")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        layout.addWidget(QLabel("已指定的下一首 · 按播放先后"))
        self.next_model = QueueListModel(numbered=True, parent=self)
        self.next_list = self._list(self.next_model)
        self.next_list.setMaximumHeight(150)
        layout.addWidget(self.next_list)
        self.next_empty = QLabel("尚未指定，按当前播放模式继续。")
        self.next_empty.setWordWrap(True)
        layout.addWidget(self.next_empty)
        self.other_label = QLabel("其他队列歌曲")
        self.other_label.setWordWrap(True)
        layout.addWidget(self.other_label)
        self.other_model = QueueListModel(parent=self)
        self.other_list = self._list(self.other_model)
        layout.addWidget(self.other_list, 1)
        self.empty_label = QLabel("")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self.empty_label, 1)
        help_label = QLabel("待播歌曲右键可移出，不删除文件或歌单；当前项不可移出。")
        help_label.setWordWrap(True)
        help_label.setProperty("class", "secondary")
        layout.addWidget(help_label)
        self.action_status = QLabel("")
        self.action_status.setTextFormat(Qt.PlainText)
        self.action_status.setWordWrap(True)
        layout.addWidget(self.action_status)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        layout.addWidget(close)

    def _list(self, model):
        view = QListView(self)
        view.setModel(model)
        view.setUniformItemSizes(True)
        view.setSpacing(Tokens.SPACING_XS)
        view.setLayoutMode(QListView.Batched)
        view.setEditTriggers(QListView.NoEditTriggers)
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(lambda pos, v=view: self._show_remove_menu(v, pos))
        return view

    def _build_remove_menu(self, model, row_number):
        if not self._editable or not 0 <= row_number < len(model.rows):
            return None
        index = model.rows[row_number]["index"]
        if index in self._protected_indices:
            return None
        menu = QMenu(self)
        action = menu.addAction("移出播放队列")
        # Capture this exact snapshot, even if the menu stays open across a refresh.
        action.triggered.connect(lambda _=False, i=index, r=self._revision: self.remove_requested.emit(i, r))
        return menu

    def _show_remove_menu(self, view, pos):
        index = view.indexAt(pos)
        if not index.isValid():
            return
        menu = self._build_remove_menu(view.model(), index.row())
        if menu is not None:
            menu.exec(view.viewport().mapToGlobal(pos))
            menu.deleteLater()

    def show_pending(self, exhausted=False):
        self._editable = False
        self.notice.setText("暂时无法读取队列，请稍后重新打开。" if exhausted
                            else "正在更新队列，列表暂保留上次读取的内容…")

    def set_snapshot(self, snapshot):
        self._revision = snapshot["revision"]
        self._protected_indices = set(snapshot["protected_indices"])
        self._editable = snapshot["state"] != "loading"
        labels = {"sequential": "顺序播放", "shuffle": "随机播放",
                  "repeat_all": "列表循环", "repeat_one": "单曲循环"}
        mode = snapshot["mode"]
        self.mode_label.setText("播放模式：" + labels.get(mode, "待确认"))
        current = snapshot["current"]
        state = {"playing": "正在播放", "paused": "已暂停", "loading": "正在切换",
                 "stopped": "已停止", "unknown": "播放状态待确认"}[snapshot["state"]]
        title = current["title"] if current else "暂无正在播放的歌曲"
        artist = f" — {current['artist']}" if current and current["artist"] else ""
        self.current_label.setText(f"{state} · {title}{artist}")
        pending = set(snapshot["next_up"])
        items = snapshot["items"]
        ordered = [items[i] for i in snapshot["next_up"]]
        others = [row for row in items if row["index"] not in pending]
        self.next_model.update_rows(ordered, snapshot["playing_index"])
        scroll = self.other_list.verticalScrollBar().value()
        self.other_model.update_rows(others, snapshot["playing_index"])
        self.other_list.verticalScrollBar().setValue(scroll)
        self.next_list.setVisible(bool(ordered))
        self.next_empty.setVisible(not ordered)
        self.other_list.setVisible(bool(others))
        self.empty_label.setVisible(not items)
        self.empty_label.setText("当前歌曲未加入队列，暂无其他队列歌曲。" if current else
                                 "播放队列为空。可在歌曲上右键选择“添加到播放队列”。")
        self.other_label.setText(f"队列歌曲 · {len(others)} 首（按队列排列，含当前与已播歌曲）")
        if mode == "shuffle":
            hint = "先播放上方指定歌曲，再恢复随机播放。" if ordered else "尚未指定下一首，后续随机选择。"
            hint += "下方列表不代表随机播放先后。"
        elif mode == "repeat_one":
            hint = "先播放已指定歌曲，再循环最后播到的歌曲。" if ordered else "单曲循环当前歌曲；手动“下一首”可切歌。"
        else:
            hint = "先播放已指定歌曲，其余按当前播放模式继续。"
        self.notice.setText(hint)
