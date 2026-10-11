from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QComboBox, QPushButton, QDialogButtonBox, QMessageBox,
)
import json
import os


class BanPickConfigDialog(QDialog):
    """Ban / Pick 优先级列表编辑 - 英雄中文名选择，内部存ID"""

    def __init__(self, title: str, current_list: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        self.setMinimumHeight(500)

        # 加载所有英雄
        self._champions = self._load_champions()
        # 建立 中文名 -> id 的映射
        self._name_to_id = {c["name"]: c["id"] for c in self._champions}
        self._id_to_name = {c["id"]: c["name"] for c in self._champions}

        # 把当前的ID列表转成中文名显示
        self._item_ids = []
        for item in (current_list or []):
            # 兼容旧的字符串/数字
            try:
                cid = int(item)
                self._item_ids.append(cid)
            except (ValueError, TypeError):
                # 如果是字符串，尝试匹配
                name = str(item).strip()
                if name in self._name_to_id:
                    self._item_ids.append(self._name_to_id[name])

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("优先级列表（越靠前越优先）"))

        self.list_widget = QListWidget()
        self._refresh_list()
        lay.addWidget(self.list_widget)

        # 改成可搜索的下拉框
        add_row = QHBoxLayout()
        self.combo_champion = QComboBox()
        self.combo_champion.setEditable(True)  # 可输入搜索
        # 按名字排序
        sorted_names = sorted(self._name_to_id.keys())
        self.combo_champion.addItems(sorted_names)
        self.combo_champion.setCurrentText("")  # 默认不选中
        add_row.addWidget(self.combo_champion, 1)

        btn_add = QPushButton("添加")
        btn_add.clicked.connect(self._on_add)
        add_row.addWidget(btn_add)
        lay.addLayout(add_row)

        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._on_delete)
        lay.addWidget(btn_del)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _load_champions(self):
        """加载英雄数据，过滤掉皮肤变体"""
        try:
            # 尝试多个可能的路径
            paths = [
                "data/lol_data/champions.json",
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "lol_data", "champions.json"),
            ]
            for p in paths:
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        all_champs = json.load(f)
                    # 过滤：只保留正式英雄（ID < 1000，60xxx都是皮肤变体）
                    return [c for c in all_champs if c['id'] < 1000]
        except Exception as e:
            print(f"[UI] 加载英雄数据失败: {e}")
        return []

    def _refresh_list(self):
        self.list_widget.clear()
        for cid in self._item_ids:
            name = self._id_to_name.get(cid, f"未知({cid})")
            QListWidgetItem(f"{name} (ID:{cid})", self.list_widget)

    def _on_add(self):
        text = self.combo_champion.currentText().strip()
        if not text:
            return

        # 精确匹配中文名
        if text in self._name_to_id:
            cid = self._name_to_id[text]
        else:
            # 模糊匹配第一个
            found = None
            for name, c in self._name_to_id.items():
                if text in name or name in text:
                    found = (name, c)
                    break
            if found:
                cid = found[1]
            else:
                QMessageBox.information(self, "提示", f"找不到英雄: {text}")
                return

        if cid in self._item_ids:
            QMessageBox.information(self, "提示", "已在列表中")
            return
        self._item_ids.append(cid)
        self._refresh_list()
        self.combo_champion.setCurrentText("")

    def _on_delete(self):
        row = self.list_widget.currentRow()
        if row >= 0 and row < len(self._item_ids):
            self._item_ids.pop(row)
            self._refresh_list()

    def values(self) -> list:
        # 返回英雄ID列表（数字），和后端期望的 List[int] 匹配
        return list(self._item_ids)
