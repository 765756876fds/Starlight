"""计时器面板：手动添加 + 列表。"""
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)


_SKILLS = ["Q", "W", "E", "R", "闪现", "传送", "点燃", "治疗", "屏障",
           "虚弱", "疾跑", "净化", "惩戒"]
_SIDES = [("敌方", "enemy"), ("我方", "ally"), ("自己", "self")]


class CooldownPanel(QWidget):
    def __init__(self, manager=None, parent=None):
        super().__init__(parent)
        self._mgr = manager
        layout = QVBoxLayout(self)

        # 添加表单
        form_box = QGroupBox("添加计时器")
        form = QFormLayout(form_box)
        self.edit_champ = QLineEdit()
        self.edit_champ.setPlaceholderText("亚索 / 艾希 / 风男...")
        self.cmb_skill = QComboBox()
        self.cmb_skill.addItems(_SKILLS)
        self.cmb_side = QComboBox()
        for label, val in _SIDES:
            self.cmb_side.addItem(label, val)
        self.btn_add = QPushButton("添加")
        self.btn_add.setProperty("class", "primary")
        form.addRow("敌方英雄", self.edit_champ)
        form.addRow("技能", self.cmb_skill)
        form.addRow("阵营", self.cmb_side)
        form.addRow(self.btn_add)
        layout.addWidget(form_box)

        self.btn_add.clicked.connect(self._on_add)

        # 列表
        list_box = QGroupBox("当前计时器")
        lv = QVBoxLayout(list_box)
        self.list = QListWidget()
        lv.addWidget(self.list)
        self.btn_cancel_all = QPushButton("全部取消")
        lv.addWidget(self.btn_cancel_all)
        layout.addWidget(list_box)

        self.btn_cancel_all.clicked.connect(self._on_cancel_all)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    def _on_add(self):
        if self._mgr is None:
            return
        champ = self.edit_champ.text().strip() or None
        skill = self.cmb_skill.currentText()
        side = self.cmb_side.currentData()
        r = self._mgr.start_reminder(champ, skill, side=side)
        if r is not None:
            self.edit_champ.clear()
            self.refresh()

    def _on_cancel_all(self):
        if self._mgr is not None:
            self._mgr.cancel_all()
            self.refresh()

    def refresh(self):
        self.list.clear()
        if self._mgr is None:
            return
        now = time.monotonic()
        for r in self._mgr.list_active():
            remain = max(0.0, r.ready_at - now)
            side_zh = {"enemy": "敌方", "ally": "我方", "self": "自己"}.get(r.side, "")
            text = f"{side_zh} {r.champion_name} {r.skill} | 剩 {int(remain)}s"
            QListWidgetItem(text, self.list)
