"""一键连招二期：编辑/添加对话框（添加英雄、编辑连招、按键选择器）。

- KeyPickerDialog：序列按键选择器（列表点选）
- TriggerCaptureDialog：触发键捕获（按键盘按键实时捕获，QWERDF 弹警告）
- ComboEditDialog：添加/编辑连招（触发键捕获 + 备注 + 按键序列增删改 + 校验）
- AddChampionDialog：从全英雄列表搜索添加英雄（已配置英雄置灰）

校验失败不保存、对话框保持打开；保存成功由调用方触发 ComboManager.save_config()。
"""
from __future__ import annotations

from typing import List, Optional, Tuple
from app.ui.theme.colors import Colors
from app.ui.theme.tokens import Tokens


from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout,
    QWidget,
)

from app.combo.models import Combo, ComboStep, ChampionCombos
from app.combo.validation import SEQUENCE_KEYS, is_valid_sequence_key, validate_combo
from app.ui.widgets.glass_dialog import GlassDialog
from app.ui.widgets.glass_msg import glass_info, glass_question, glass_warning
from app.ui.widgets.icon_utils import lucide_icon

# 按键分组展示（触发键模式只显示 物品 + 其他）
_KEY_GROUPS = [
    ("技能", "qwer"),
    ("召唤", "df"),
    ("物品", "1234567"),
    ("其他", "abcs"),
]


class KeyPickerDialog(GlassDialog):
    """按键选择器（序列按键用，列表点选；触发键改用 TriggerCaptureDialog 捕获）。"""

    def __init__(self, parent=None, title: str = "选择按键"):
        super().__init__(title, parent)
        self._selected: Optional[str] = None
        layout = self.body_layout
        layout.addWidget(QLabel("点击按键选择"))
        for label, keys in _KEY_GROUPS:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{label}:"))
            for k in keys:
                if k not in SEQUENCE_KEYS:
                    continue
                btn = QPushButton(k.upper())
                # 显式 sizePolicy：最小宽度被尊重，不被容器压缩
                btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                btn.setMinimumWidth(52)
                btn.setFixedHeight(30)
                btn.clicked.connect(lambda _=False, kk=k: self._pick(kk))
                row.addWidget(btn)
            row.addStretch()
            layout.addLayout(row)
        # 手动输入：按钮列表只覆盖常用键，其余字母/数字可在此输入（1~5 位）
        tip = QLabel("或手动输入按键（1~5 位字母/数字）：")
        layout.addWidget(tip)
        mrow = QHBoxLayout()
        self.edt_manual = QLineEdit()
        self.edt_manual.setPlaceholderText("如 s / g / w")
        self.edt_manual.setMaxLength(5)
        self.edt_manual.returnPressed.connect(self._pick_manual)
        btn_manual = QPushButton("确定")
        btn_manual.setProperty("class", "primary")
        btn_manual.clicked.connect(self._pick_manual)
        mrow.addWidget(self.edt_manual, 1)
        mrow.addWidget(btn_manual)
        layout.addLayout(mrow)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        layout.addWidget(btn_cancel)

    def _pick(self, key: str) -> None:
        self._selected = key
        self.accept()

    def _pick_manual(self) -> None:
        k = self.edt_manual.text().strip().lower()
        if is_valid_sequence_key(k):
            self._selected = k
            self.accept()
        else:
            glass_warning(self, "按键不合法", "需 1~5 位字母或数字（如 s / g / w）")

    def selected_key(self) -> Optional[str]:
        return self._selected


class TriggerCaptureDialog(GlassDialog):
    """触发键捕获：按键盘按键实时捕获。

    - 允许：字母 a-z / 主键区数字 0-9（统一转小写）
    - 不支持：Shift/Ctrl/Alt/Win/F1-F12/Tab/Esc/方向键/小键盘等 → 提示重按
    - QWERDF（游戏常用键）→ 弹警告，确认才用
    - 未捕获到键之前 [确定] 置灰
    """

    _captured_signal = Signal(str)  # 跨线程（keyboard 回调线程 → UI 线程）

    def __init__(self, parent=None):
        super().__init__("选择触发键", parent)
        self._captured: Optional[str] = None
        self._remove_hook = None

        layout = self.body_layout
        layout.addWidget(QLabel("请按下你想用的触发键"))
        self.lbl_display = QLabel("—")
        self.lbl_display.setAlignment(Qt.AlignCenter)
        self.lbl_display.setStyleSheet(
            "font-size: 30px; font-weight: bold; padding: 6px;")
        layout.addWidget(self.lbl_display)
        self.lbl_hint = QLabel("支持字母与主键盘数字；Shift/Ctrl/F1 等特殊键不支持")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        layout.addWidget(self.lbl_hint)

        btns = QHBoxLayout()
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self.accept)
        btns.addStretch()
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_ok)
        layout.addLayout(btns)

        self._captured_signal.connect(self._on_captured)
        try:
            import keyboard
            self._remove_hook = keyboard.hook(self._on_key)
        except Exception as e:
            self.lbl_hint.setText(f"键盘捕获不可用: {e}")

    # ---------- 捕获解析 ----------

    @staticmethod
    def normalize_key(name: str) -> Optional[str]:
        """键盘事件键名 → 合法触发键（小写）；None=不支持。

        只认单字符：字母 a-z / 主键盘数字 0-9（小键盘 num 1 等名字更长 → None）。
        """
        n = (name or "").lower()
        if len(n) == 1 and (n.isalpha() or n.isdigit()):
            return n
        return None

    def _on_key(self, e) -> None:
        """keyboard 回调（独立线程）→ 通过 Signal 回 UI 线程。"""
        if getattr(e, "event_type", None) != "down":
            return
        key = self.normalize_key(getattr(e, "name", "") or "")
        if key is None:
            self._captured_signal.emit("__unsupported__")
        else:
            self._captured_signal.emit(key)

    def _on_captured(self, key: str) -> None:
        if key == "__unsupported__":
            self._captured = None
            self.btn_ok.setEnabled(False)
            self.lbl_display.setText("—")
            self.lbl_hint.setText("该键不支持，请重新按（支持字母与主键盘数字）")
            return
        if key in ("q", "w", "e", "r", "d", "f"):
            if not glass_question(self, "确认触发键",
                                  f"触发键 {key.upper()} 是游戏常用键，可能误触发，确认要用吗？"):
                self.lbl_hint.setText("已取消，请重新按其他键")
                return
        self._captured = key
        self.lbl_display.setText(key.upper())
        self.lbl_hint.setText("已捕获，点击确定")
        self.btn_ok.setEnabled(True)

    def selected_key(self) -> Optional[str]:
        return self._captured

    def done(self, result: int) -> None:
        """关闭时精确取消自己的 keyboard hook（不影响其他 hook）。"""
        try:
            if self._remove_hook is not None:
                import keyboard
                keyboard.unhook(self._remove_hook)
                self._remove_hook = None
        except Exception:
            pass
        super().done(result)


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

    btn_key = QPushButton(step.key.upper())
    # 显式 sizePolicy：水平 Minimum（最小宽度被尊重），垂直 Fixed
    btn_key.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    btn_key.setMinimumWidth(52)
    btn_key.setFixedHeight(30)

    def _pick_key():
        dlg = KeyPickerDialog(dialog, title="选择序列按键")
        if dlg.exec() == QDialog.Accepted and dlg.selected_key():
            step.key = dlg.selected_key()
            btn_key.setText(step.key.upper())

    btn_key.clicked.connect(lambda: _pick_key())
    h.addWidget(btn_key)

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

        # 触发键
        trig_row = QHBoxLayout()
        trig_row.addWidget(QLabel("触发键："))
        self.btn_trigger = QPushButton(
            (combo.trigger.upper() if combo and combo.trigger else "点击选择"))
        self.btn_trigger.clicked.connect(self._pick_trigger)
        trig_row.addWidget(self.btn_trigger)
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
        self.steps_layout.addStretch(1)  # 底部弹性：行少时顶部对齐，行不被垂直拉伸

    def _remove_step(self, step: ComboStep) -> None:
        if step in self._steps:
            self._steps.remove(step)
        self._rebuild_steps()

    def _add_step(self) -> None:
        dlg = KeyPickerDialog(self, title="选择序列按键")
        if dlg.exec() != QDialog.Accepted or not dlg.selected_key():
            return
        self._steps.append(ComboStep(key=dlg.selected_key(), delay_after_ms=50))
        self._rebuild_steps()

    def _pick_trigger(self) -> None:
        """触发键：按键捕获（不是列表点选）。"""
        dlg = TriggerCaptureDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_key():
            self.btn_trigger.setText(dlg.selected_key().upper())
            self._pending_trigger = dlg.selected_key()
        else:
            self._pending_trigger = None

    # ---------- 保存 ----------

    def _save(self) -> None:
        trigger = (getattr(self, "_pending_trigger", None)
                   or (self._combo.trigger if self._combo else ""))
        trigger = (trigger or "").strip().lower()
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
