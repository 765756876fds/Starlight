from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QLineEdit, QPushButton, QDialogButtonBox, QMessageBox,
)


class BanPickConfigDialog(QDialog):
    """Ban / Pick 优先级列表编辑"""

    def __init__(self, title: str, current_list: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(360)
        self._items = list(current_list or [])

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("优先级列表（越靠前越优先）"))

        self.list_widget = QListWidget()
        self._refresh_list()
        lay.addWidget(self.list_widget)

        add_row = QHBoxLayout()
        self.edit_add = QLineEdit()
        self.edit_add.setPlaceholderText("英雄名（中文/拼音/ID）")
        btn_add = QPushButton("添加")
        btn_add.clicked.connect(self._on_add)
        add_row.addWidget(self.edit_add)
        add_row.addWidget(btn_add)
        lay.addLayout(add_row)

        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._on_delete)
        lay.addWidget(btn_del)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _refresh_list(self):
        self.list_widget.clear()
        for name in self._items:
            QListWidgetItem(str(name), self.list_widget)

    def _on_add(self):
        text = self.edit_add.text().strip()
        if not text:
            return
        if text in self._items:
            QMessageBox.information(self, "提示", "已在列表中")
            return
        self._items.append(text)
        self._refresh_list()
        self.edit_add.clear()

    def _on_delete(self):
        row = self.list_widget.currentRow()
        if row >= 0 and row < len(self._items):
            self._items.pop(row)
            self._refresh_list()

    def values(self) -> list:
        return list(self._items)
