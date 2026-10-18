from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QLineEdit, QPushButton,
)
import json
import os

from ..widgets.glass_dialog import GlassDialog
from ..widgets.glass_msg import glass_info
from ..widgets.icon_utils import lucide_icon
from ..theme.tokens import Tokens

# 别名表（俗称/本名 → 称号）：导入失败则退化为 中文名/英文名/称号 三字段匹配
try:
    from app.lol.champion.normalizer import ALIASES as _ALIASES
except Exception:
    _ALIASES = {}


class BanPickConfigDialog(GlassDialog):
    """Ban / Pick 优先级列表编辑 - 搜索式英雄选择（中文/英文/称号/俗称），内部存ID"""

    def __init__(self, title: str, current_list: list, parent=None):
        super().__init__(title, parent)
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

        lay = self.body_layout
        lay.addWidget(QLabel("优先级列表（越靠前越优先）"))

        self.list_widget = QListWidget()
        self._refresh_list()
        lay.addWidget(self.list_widget)

        # 搜索式英雄选择：输入即过滤，双击/回车添加（与连招面板交互一致）
        lay.addWidget(QLabel("选择英雄（搜索过滤，双击/回车添加）"))
        self.edt_search = QLineEdit()
        self.edt_search.setPlaceholderText("搜索英雄（中文/英文/别名）")
        self.edt_search.textChanged.connect(self._refresh_candidates)
        lay.addWidget(self.edt_search)

        self.lst_candidates = QListWidget()
        # itemActivated：双击或回车添加（避免单击浏览时误触）
        self.lst_candidates.itemActivated.connect(self._on_pick)
        lay.addWidget(self.lst_candidates, 1)

        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._on_delete)
        lay.addWidget(btn_del)

        # 底部按钮：中文 确定/取消，确定挂 primary 蓝实底（与全站 GlassDialog 一致）
        row = QHBoxLayout()
        row.setSpacing(Tokens.SPACING_SM)
        row.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.setMinimumWidth(88)
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("确定")
        btn_ok.setProperty("class", "primary")
        btn_ok.setMinimumWidth(88)
        btn_ok.clicked.connect(self.accept)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        lay.addLayout(row)

        self._refresh_candidates()

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

    def _hay(self, ch) -> str:
        """匹配文本源：中文名 + 英文名 + 称号 + 该英雄的俗称/本名别名（v == title 判断归属）。"""
        parts = [ch.get("name", ""), ch.get("alias", ""), ch.get("title", "")]
        title = ch.get("title", "")
        for k, v in _ALIASES.items():
            if v == title:
                parts.append(k)
        return " ".join(parts).lower()

    def _refresh_candidates(self):
        """按搜索词过滤候选英雄列表（双击/回车添加）。"""
        kw = self.edt_search.text().strip().lower()
        self.lst_candidates.blockSignals(True)
        self.lst_candidates.clear()
        for ch in self._champions:
            if kw and kw not in self._hay(ch):
                continue
            name = ch.get("name", "")
            title = ch.get("title", "")
            label = f"{name}（{title}）" if title else name
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, ch["id"])
            self.lst_candidates.addItem(item)
        self.lst_candidates.blockSignals(False)

    def _refresh_list(self):
        self.list_widget.clear()
        for cid in self._item_ids:
            name = self._id_to_name.get(cid, f"未知({cid})")
            QListWidgetItem(f"{name} (ID:{cid})", self.list_widget)

    def _on_pick(self, item):
        cid = item.data(Qt.UserRole)
        if cid in self._item_ids:
            glass_info(self, "提示", "已在列表中")
            return
        self._item_ids.append(cid)
        self._refresh_list()
        # clear() 会触发 textChanged → _refresh_candidates，无需手动再调
        self.edt_search.clear()

    def _on_delete(self):
        row = self.list_widget.currentRow()
        if row >= 0 and row < len(self._item_ids):
            self._item_ids.pop(row)
            self._refresh_list()

    def values(self) -> list:
        # 返回英雄ID列表（数字），和后端期望的 List[int] 匹配
        return list(self._item_ids)
