"""一键连招子面板（一期：只读展示，不做连招编辑）。

- 总开关（默认关闭，需手动开启）
- 搜索英雄（中文名 / 英文名 / 拼音别名模糊匹配）
- 英雄列表：盲僧固定第 1、锤石固定第 2，其余按使用频率降序
- 右侧连招详情：trigger + note + 按键序列
- 底部显示当前对局英雄（自动识别自 Ongoing）
"""
from __future__ import annotations

import json
from app.ui.theme.colors import Colors
from app.ui.theme.tokens import Tokens
from app.ui.widgets.icon_utils import lucide_icon

import os
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CHAMPIONS_JSON = os.path.join(PROJECT_ROOT, "data", "lol_data", "champions.json")

# 固定置顶的两个英雄（施工指令：盲僧第 1、锤石第 2）
PINNED_IDS = [64, 412]


class ComboPanel(QWidget):
    def __init__(self, combo_manager=None, parent=None):
        super().__init__(parent)
        self._combo = combo_manager
        self._champions: List[Tuple[int, str, str]] = []  # (id, name, alias)
        self._item_to_cid: Dict[str, int] = {}
        self._load_champions()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        box_title = QLabel("一键连招")
        box_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(box_title)

        self.edt_search = QLineEdit()
        self.edt_search.setPlaceholderText("搜索英雄（中文/英文/别名）")
        self.edt_search.textChanged.connect(self._refresh_list)
        search_row = QHBoxLayout()
        search_row.setSpacing(Tokens.SPACING_SM)
        lbl_search = QLabel()
        lbl_search.setPixmap(lucide_icon("search", 14, Colors.TEXT_SECONDARY).pixmap(14, 14))
        lbl_search.setFixedSize(14, 14)
        search_row.addWidget(lbl_search)      # Lucide search 图标（搜索框左侧）
        search_row.addWidget(self.edt_search, 1)
        self.btn_add_champion = QPushButton("添加英雄")
        self.btn_add_champion.setIcon(lucide_icon("plus", 14, Colors.TEXT_PRIMARY))
        self.btn_add_champion.setMinimumWidth(96)  # 自适应，不写死
        self.btn_add_champion.clicked.connect(self._on_add_champion)
        search_row.addWidget(self.btn_add_champion)
        layout.addLayout(search_row)

        body = QHBoxLayout()
        # 左列：英雄列表 + [删除此英雄]（普通布局，不做进列表 item，避免宽度压缩）
        left_col = QVBoxLayout()
        self.lst_heroes = QListWidget()
        self.lst_heroes.currentItemChanged.connect(self._on_select)
        left_col.addWidget(self.lst_heroes, 1)
        self.btn_delete_champion = QPushButton("删除此英雄")
        self.btn_delete_champion.setObjectName("btn_delete_champion")  # 危险操作样式（QSS）
        self.btn_delete_champion.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_delete_champion.setMinimumWidth(88)
        self.btn_delete_champion.clicked.connect(self._on_delete_champion)
        left_col.addWidget(self.btn_delete_champion)
        body.addLayout(left_col, 3)

        self.detail = QWidget()
        self._detail_layout = QVBoxLayout(self.detail)
        self._detail_layout.setContentsMargins(Tokens.SPACING_XS, 0, 0, 0)
        self._detail_layout.setAlignment(Qt.AlignTop)  # 内容顶对齐，不留垂直居中
        self.lbl_detail_champ = QLabel("当前英雄：-")
        self.lbl_detail_champ.setStyleSheet(
            f"font-size: {Tokens.FONT_SIZE_TITLE}px; font-weight: bold;")
        self._detail_layout.addWidget(self.lbl_detail_champ)
        body.addWidget(self.detail, 4)
        layout.addLayout(body, 1)

        self.lbl_current = QLabel("当前对局英雄：-（自动识别）")
        self.lbl_current.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        layout.addWidget(self.lbl_current)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_current)
        self._timer.start(2000)

        self._refresh_list()
        self._refresh_current()

    # ---------- 数据 ----------

    def _load_champions(self) -> None:
        try:
            with open(CHAMPIONS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            for ch in data:
                cid = int(ch.get("id") or 0)
                name = ch.get("name") or ""
                alias = ch.get("alias") or ""
                if cid and name:
                    self._champions.append((cid, name, alias))
        except Exception as e:
            print(f"[Combo] 加载 champions.json 失败: {e}")
            self._champions = []

    def _sorted_champions(self) -> List[Tuple[int, str, str]]:
        """排序：盲僧/锤石固定前二，其余按使用频率降序。"""
        usage = {
            cid: self._combo.get_usage(cid) if self._combo else 0
            for cid, _, _ in self._champions
        }
        pinned = [(cid, n, a) for cid, n, a in self._champions if cid in PINNED_IDS]
        pinned_sorted = sorted(
            pinned, key=lambda x: PINNED_IDS.index(x[0]))
        rest = [(cid, n, a) for cid, n, a in self._champions if cid not in PINNED_IDS]
        rest_sorted = sorted(rest, key=lambda x: -usage.get(x[0], 0))
        return pinned_sorted + rest_sorted

    def _matches(self, cid: int, name: str, alias: str, kw: str) -> bool:
        if not kw:
            return True
        hay = f"{name} {alias}".lower()
        cc = self._combo.find_champion_by_id(cid) if self._combo else None
        if cc is not None:
            hay += " " + " ".join(cc.aliases)
        return kw in hay

    def _refresh_list(self) -> None:
        kw = self.edt_search.text().strip().lower()
        cur = self.lst_heroes.currentItem()
        cur_cid = self._item_to_cid.get(cur.text()) if cur else None
        self.lst_heroes.blockSignals(True)
        self.lst_heroes.clear()
        self._item_to_cid = {}
        cur_champ_id = self._combo.current_champion_id() if self._combo else None
        for cid, name, alias in self._sorted_champions():
            if not self._matches(cid, name, alias, kw):
                continue
            has_combo = bool(self._combo and self._combo.find_champion_by_id(cid)
                             and self._combo.find_champion_by_id(cid).combos)
            mark = "● " if cid == cur_champ_id else ("✓ " if has_combo else "  ")
            item = QListWidgetItem(f"{mark}{name}")
            item.setData(Qt.UserRole, cid)
            self.lst_heroes.addItem(item)
            self._item_to_cid[item.text()] = cid
        self.lst_heroes.blockSignals(False)
        # 尽量恢复选中
        if cur_cid is not None:
            for i in range(self.lst_heroes.count()):
                if self.lst_heroes.item(i).data(Qt.UserRole) == cur_cid:
                    self.lst_heroes.setCurrentRow(i)
                    break
        elif self.lst_heroes.count() > 0:
            self.lst_heroes.setCurrentRow(0)

    def _refresh_current(self) -> None:
        if not self._combo:
            return
        name = self._combo.current_champion_name()
        if not (isinstance(name, str) and name):
            name = "-"   # 非真实字符串（None/异常/mock）不展示
        self.lbl_current.setText(
            f"当前对局英雄：{name}（自动识别）")
        # 当前英雄若有连招配置，自动滚动/标记（列表刷新已处理 ● 标记）
        self._refresh_list()

    # ---------- 交互 ----------

    def _on_select(self, cur: Optional[QListWidgetItem], _prev=None) -> None:
        if cur is None:
            return
        cid = cur.data(Qt.UserRole) or 0
        self._show_detail(cid)
        if self._combo and cid:
            self._combo.bump_usage(cid)

    def _show_detail(self, cid: int) -> None:
        # 清空详情区（保留标题 label，后续重建内容）
        while self._detail_layout.count() > 1:
            item = self._detail_layout.takeAt(1)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        cc = self._combo.find_champion_by_id(cid) if self._combo else None
        if cc is None:
            self.lbl_detail_champ.setText("当前英雄：-")
            if self._combo and not self._combo.get_champions():
                hint = QLabel("暂无英雄，点击「+ 添加英雄」添加")
            else:
                hint = QLabel("该英雄暂无连招配置\n（点击上方 [+ 添加英雄] 加入连招库）")
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
            self._detail_layout.addWidget(hint)
            self._detail_layout.addStretch(1)  # 剩余空间留在底部，内容顶对齐
            return
        self.lbl_detail_champ.setText(f"当前英雄：{cc.name}")
        if not cc.combos:
            hint = QLabel("暂无连招，点击下方 [+ 添加连招] 创建")
            hint.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
            self._detail_layout.addWidget(hint)
        else:
            for combo in cc.combos:
                row = QWidget()
                row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)  # 垂直不拉伸
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 2, 0, 2)
                lbl = QLabel(f"[{combo.trigger.upper()}] {combo.note}"
                             f"（{combo.summary}）")
                lbl.setWordWrap(True)
                lbl.setStyleSheet("font-size: {Tokens.FONT_SIZE_SUB}px;")
                h.addWidget(lbl, 1)
                btn = QPushButton("编辑")
                # 显式 sizePolicy：水平 Minimum（最小宽度被尊重），不被容器压缩
                btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                btn.setMinimumWidth(56)
                btn.setFixedHeight(28)
                btn.clicked.connect(lambda _=False, c=combo:
                                    self._on_edit_combo(cc, c))
                h.addWidget(btn)
                btn_del = QPushButton()
                btn_del.setIcon(lucide_icon("x", 13, Colors.DANGER))
                # 删除连招按钮：红色醒目；sizePolicy 保证不被压缩
                btn_del.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                btn_del.setMinimumWidth(32)
                btn_del.setFixedHeight(28)
                btn_del.setToolTip("删除此连招")
                btn_del.clicked.connect(lambda _=False, c=combo:
                                        self._on_delete_combo(cc, c))
                h.addWidget(btn_del)
                self._detail_layout.addWidget(row)
        btn_add = QPushButton("+ 添加连招")
        btn_add.clicked.connect(lambda _=False: self._on_add_combo(cc))
        self._detail_layout.addWidget(btn_add)
        self._detail_layout.addStretch(1)  # 剩余空间留在底部，内容从上往下排

    # ---------- 二期：添加英雄 / 编辑连招 ----------

    def _select_cid(self, cid: int) -> None:
        for i in range(self.lst_heroes.count()):
            if self.lst_heroes.item(i).data(Qt.UserRole) == cid:
                self.lst_heroes.setCurrentRow(i)
                return

    def _on_add_champion(self) -> None:
        if not self._combo:
            return
        from app.ui.dialogs.combo_dialogs import AddChampionDialog
        existing = [c.champion_id for c in self._combo.get_champions()]
        dlg = AddChampionDialog(
            [(cid, name, alias, "") for cid, name, alias in self._champions],
            existing, parent=self)
        if dlg.exec() != QDialog.Accepted or not dlg.selected:
            return
        cid, name = dlg.selected
        cc = self._combo.add_champion(name, cid)
        if cc is None:
            return
        print(f"[Combo] 已添加英雄: {cc.name}（id={cc.champion_id}）")
        self._refresh_list()
        self._select_cid(cc.champion_id)

    def _on_edit_combo(self, champ, combo) -> None:
        from app.ui.dialogs.combo_dialogs import ComboEditDialog
        dlg = ComboEditDialog(champ, combo, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self._after_save(champ)

    def _on_add_combo(self, champ) -> None:
        from app.ui.dialogs.combo_dialogs import ComboEditDialog
        dlg = ComboEditDialog(champ, None, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self._after_save(champ)

    def _after_save(self, champ) -> None:
        """保存后：写回 combos.yaml + reload + 刷新 UI。"""
        if self._combo and self._combo.save_config():
            print("[Combo] 配置已保存")
        self._refresh_current()
        self._show_detail(champ.champion_id)

    # ---------- 二期第二步：删除 ----------

    def _confirm_delete(self, title: str, text: str) -> bool:
        """二次确认框：默认焦点在[取消]，[确定删除]为红色警示按钮。"""
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        btn_cancel = box.addButton("取消", QMessageBox.RejectRole)
        btn_del = box.addButton("确定删除", QMessageBox.DestructiveRole)
        btn_del.setStyleSheet(
            f"background-color: {Colors.DANGER}; color: white; font-weight: bold;")
        box.setDefaultButton(btn_cancel)   # 防手滑回车误删
        box.setEscapeButton(btn_cancel)
        box.exec()
        return box.clickedButton() is btn_del

    def _on_delete_combo(self, champ, combo) -> None:
        """删连招：只删连招保留英雄 → save_config 保存 → 刷新。"""
        if not self._confirm_delete(
                "删除连招",
                f"确定删除连招 [{combo.trigger.upper()}] {combo.note}？\n"
                "此操作不可恢复。"):
            return
        if combo in champ.combos:
            champ.combos.remove(combo)
        if self._combo and self._combo.save_config():
            print(f"[Combo] 已删除连招 [{combo.trigger.upper()}] {combo.note}")
        self._refresh_current()
        self._show_detail(champ.champion_id)

    def _on_delete_champion(self) -> None:
        """删英雄：连它所有连招一起删 → 保存 → 刷新；库空时显示提示。"""
        cur = self.lst_heroes.currentItem()
        if cur is None:
            return
        cid = cur.data(Qt.UserRole) or 0
        cc = self._combo.find_champion_by_id(cid) if self._combo else None
        if cc is None:
            return
        n = len(cc.combos)
        sub = f"它的 {n} 个连招会一起删除，不可恢复。" if n else "它没有连招。"
        if not self._confirm_delete(
                "删除英雄",
                f"确定删除英雄【{cc.name}】？\n{sub}"):
            return
        if self._combo and self._combo.remove_champion(cid):
            print(f"[Combo] 已删除英雄: {cc.name}")
        self._refresh_current()  # 内部会 refresh_list
        if not (self._combo and self._combo.get_champions()):
            # 库空：清空详情显示"暂无英雄"
            self._show_detail(0)
        elif self.lst_heroes.count() > 0:
            self.lst_heroes.setCurrentRow(0)
