"""一键连招二期：编辑/添加对话框（添加英雄、编辑连招）。

- ComboEditDialog：添加/编辑连招（SingleKeyEdit 按键捕获 + 备注 + 按键序列增删改 + 校验）
- AddChampionDialog：从全英雄列表搜索添加英雄（已配置英雄置灰）

按键捕获改用官方 QKeySequenceEdit（SingleKeyEdit 子类），废弃全局钩子弹窗
TriggerCaptureDialog / KeyPickerDialog；校验失败不保存、对话框保持打开；
保存成功由调用方触发 ComboManager.save_config()。
"""
from __future__ import annotations

from typing import List, Optional, Tuple
from app.ui.theme.colors import Colors
from app.ui.theme.tokens import Tokens


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout,
    QWidget,
)

from app.combo.models import Combo, ComboStep, ChampionCombos
from app.combo.validation import validate_combo
from app.ui.widgets.glass_dialog import GlassDialog
from app.ui.widgets.glass_msg import glass_info, glass_warning
from app.ui.widgets.icon_utils import lucide_icon
from app.ui.widgets.single_key_edit import SingleKeyEdit


def _make_step_row(dialog: "ComboEditDialog", index: int,
                   step: ComboStep) -> QWidget:
    """序列行控件：序号 + 键按钮 + 延迟 SpinBox + 删除按钮。

    直接放入 QScrollArea 的 QVBoxLayout（不经过 QListWidget item 宽度压缩）。
    行内控件显式 sizePolicy，保证最小宽度被尊重。
    """
    row = QWidget()
    row.setObjectName("combo_step_card")   # 半透明圆角卡片（QSS #combo_step_card）
    row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)  # 垂直不拉伸
    h = QHBoxLayout(row)
    h.setContentsMargins(Tokens.SPACING_XS, 2, Tokens.SPACING_XS, 2)
    h.setSpacing(Tokens.SPACING_SM)

    lbl_idx = QLabel(f"{index}.")
    lbl_idx.setMinimumWidth(22)
    lbl_idx.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    h.addWidget(lbl_idx)

    key_edit = SingleKeyEdit()
    key_edit.set_value(step.key)
    key_edit.setMinimumWidth(80)
    key_edit.setFixedHeight(30)
    key_edit.keySequenceChanged.connect(
        lambda: setattr(step, "key", key_edit.value()))
    h.addWidget(key_edit)

    spin = QSpinBox()
    spin.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    spin.setRange(0, 10000)
    spin.setValue(int(step.delay_after_ms or 0))
    spin.setSuffix(" ms")
    spin.setMinimumWidth(90)  # 保底宽度，随内容自适应（"50 ms" 完整显示）
    spin.valueChanged.connect(lambda v, s=step: setattr(s, "delay_after_ms", int(v)))
    h.addWidget(spin)

    btn_del = QPushButton()
    btn_del.setIcon(lucide_icon("x", 13, Colors.TEXT_SECONDARY))
    # 显式 sizePolicy：水平 Minimum（最小宽度被尊重），垂直 Fixed
    btn_del.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    btn_del.setMinimumWidth(32)
    btn_del.setFixedHeight(30)
    btn_del.setToolTip("删除该步骤")
    btn_del.clicked.connect(lambda: dialog._remove_step(step))
    h.addWidget(btn_del)

    h.addStretch(1)  # 拉伸放最后：控件组靠左完整显示，不挤压 ×
    return row


class ComboEditDialog(GlassDialog):
    """添加/编辑连招。combo=None → 添加模式；否则编辑现有 combo（直接改其字段）。"""

    def __init__(self, champ: ChampionCombos, combo: Optional[Combo] = None,
                 parent=None):
        super().__init__("编辑连招" if combo is not None else "添加连招", parent)
        self._champ = champ
        self._combo = combo
        self._steps: List[ComboStep] = (
            list(combo.sequence) if combo is not None else [])

        layout = self.body_layout

        # 触发键（SingleKeyEdit：点击后直接按键盘即可捕获回填，无弹窗）
        trig_row = QHBoxLayout()
        trig_row.addWidget(QLabel("触发键："))
        self.trigger_edit = SingleKeyEdit()
        self.trigger_edit.set_value(combo.trigger if combo else "")
        self.trigger_edit.setMinimumWidth(90)
        self.trigger_edit.setFixedHeight(30)
        trig_row.addWidget(self.trigger_edit)
        trig_row.addStretch()
        layout.addLayout(trig_row)

        # 备注
        note_row = QHBoxLayout()
        note_row.addWidget(QLabel("连招备注："))
        self.edt_note = QLineEdit(combo.note if combo else "")
        self.edt_note.setPlaceholderText("如：插眼-闪现-摸眼")
        note_row.addWidget(self.edt_note, 1)
        layout.addLayout(note_row)

        # 按键序列（QScrollArea + QVBoxLayout：行不经过 QListWidget item 宽度压缩）
        layout.addWidget(QLabel("按键序列（按顺序执行）："))
        self.scroll_steps = QScrollArea()
        self.scroll_steps.setWidgetResizable(True)  # 内容随窗口伸缩
        self.scroll_steps.setFrameShape(QFrame.StyledPanel)
        self.steps_container = QWidget()
        self.steps_layout = QVBoxLayout(self.steps_container)
        self.steps_layout.setContentsMargins(Tokens.SPACING_XS, Tokens.SPACING_XS,
                                             Tokens.SPACING_XS, Tokens.SPACING_XS)
        self.steps_layout.setSpacing(Tokens.SPACING_XS)
        self.scroll_steps.setWidget(self.steps_container)
        layout.addWidget(self.scroll_steps, 1)
        self._rebuild_steps()

        btn_add = QPushButton("添加一步")
        btn_add.setIcon(lucide_icon("plus", 14, Colors.TEXT_PRIMARY))
        btn_add.clicked.connect(self._add_step)
        layout.addWidget(btn_add)

        btns = QHBoxLayout()
        btns.setSpacing(Tokens.SPACING_SM)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("保存")
        btn_save.setProperty("class", "primary")
        btn_save.clicked.connect(self._save)
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_save)
        layout.addLayout(btns)

    # ---------- 序列编辑 ----------

    def _rebuild_steps(self) -> None:
        """重建序列行：每行只创建一次 widget，直接 addWidget 进 QVBoxLayout。"""
        while self.steps_layout.count():
            item = self.steps_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for i, step in enumerate(self._steps, 1):
            self.steps_layout.addWidget(_make_step_row(self, i, step))
        self.steps_layout.addStretch(1)
        # 新步骤行追加在底部：自动滚动到底，避免"加不进去"的错觉
        self.scroll_steps.verticalScrollBar().setValue(
            self.scroll_steps.verticalScrollBar().maximum())  # 底部弹性：行少时顶部对齐，行不被垂直拉伸

    def _remove_step(self, step: ComboStep) -> None:
        if step in self._steps:
            self._steps.remove(step)
        self._rebuild_steps()

    def _add_step(self) -> None:
        """添加一步：直接追加空步骤行（无弹窗），行内 SingleKeyEdit 点击即录入。"""
        self._steps.append(ComboStep(key="", delay_after_ms=50))
        self._rebuild_steps()


    # ---------- 保存 ----------

    def _save(self) -> None:
        trigger = self.trigger_edit.value().lower()
        if not trigger:
            glass_warning(self, "校验失败", "触发键不能为空")
            return
        for i, s in enumerate(self._steps, 1):
            if not (getattr(s, "key", "") or "").strip():
                glass_warning(self, "校验失败", f"第 {i} 步按键不能为空")
                return
        ok, errors = validate_combo(
            self._champ, trigger, self._steps, exclude=self._combo)
        if not ok:
            glass_warning(self, "校验失败", "\n".join(errors))
            return
        note = self.edt_note.text().strip()
        if self._combo is not None:
            self._combo.trigger = trigger
            self._combo.note = note
            self._combo.sequence = list(self._steps)
        else:
            self._champ.combos.append(
                Combo(trigger=trigger, note=note, sequence=list(self._steps)))
        self.accept()


class AddChampionDialog(GlassDialog):
    """添加英雄：全英雄搜索（中文名 / 英文名 / title），已配置英雄置灰不可选。"""

    def __init__(self, champions: List[Tuple[int, str, str, str]],
                 existing_ids: List[int], parent=None):
        super().__init__("添加英雄", parent)
        self._champions = champions  # (cid, name, alias, title)
        self._existing = set(int(x) for x in existing_ids)
        self.selected: Optional[Tuple[int, str]] = None  # (cid, name)

        layout = self.body_layout
        self.edt_search = QLineEdit()
        self.edt_search.setPlaceholderText("搜索（中文/英文/别名）")
        self.edt_search.textChanged.connect(self._refresh)
        layout.addWidget(self.edt_search)

        self.lst = QListWidget()
        layout.addWidget(self.lst, 1)

        btns = QHBoxLayout()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("确定")
        btn_ok.setProperty("class", "primary")
        btn_ok.clicked.connect(self._confirm)
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        layout.addLayout(btns)

        self._refresh()

    def _refresh(self) -> None:
        kw = self.edt_search.text().strip().lower()
        self.lst.clear()
        for cid, name, alias, title in sorted(
                self._champions, key=lambda x: x[1]):
            if not self._matches(name, alias, title, kw):
                continue
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, cid)
            if cid in self._existing:
                item.setText(f"{name}（已添加）")
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)  # 置灰不可选
            self.lst.addItem(item)

    @staticmethod
    def _matches(name: str, alias: str, title: str, kw: str) -> bool:
        if not kw:
            return True
        hay = f"{name} {alias} {title}".lower().replace(" ", "")
        needle = kw.replace(" ", "")
        return needle in hay

    def _confirm(self) -> None:
        item = self.lst.currentItem()
        if item is None:
            glass_info(self, "添加英雄", "请先选择要添加的英雄")
            return
        cid = item.data(Qt.UserRole) or 0
        self.selected = (int(cid), item.text().replace("（已添加）", ""))
        self.accept()
