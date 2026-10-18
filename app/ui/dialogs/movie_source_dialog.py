# -*- coding: utf-8 -*-
"""电影源管理对话框：多源列表 + 增删 + 测源（三合一）+ 设为当前。仿音源管理。"""
from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from ...movie.source_manager import MovieSourceManager
from ...ui.widgets.glass_dialog import GlassDialog
from ...ui.widgets.glass_msg import glass_info, glass_question, glass_warning


class _SourceTestWorker(QObject):
    """后台批量测源（三合一），逐个 done(row, name, ok, detail)。"""

    done = Signal(int, str, bool, str)

    def __init__(self, manager, sources):
        super().__init__()
        self._manager = manager
        self._sources = sources  # [(index, src), ...]

    def run(self):
        for idx, src in self._sources:
            ok, detail = self._manager.test_source(src)
            self.done.emit(idx, src.get("name") or "", ok, detail)


class MovieSourceDialog(GlassDialog):
    """电影源管理：状态 + 名称 + URL + 勾选启用 + 设为当前 + 测试。"""

    def __init__(self, parent=None, manager=None, on_sources_changed=None):
        super().__init__("电影源管理", parent)
        self.resize(680, 440)
        self.manager = manager if manager is not None else MovieSourceManager()
        self.on_sources_changed = on_sources_changed
        self._loading = False
        self._test_thread = None
        self._test_worker = None

        root = self.body_layout

        tip = QLabel("电影源（苹果 CMS V10 接口）。勾选启用、双击行切换；"
                     "测试为三合一（连通 / 搜索有结果 / m3u8 可播）")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.list_widget = QListWidget()
        root.addWidget(self.list_widget, 1)

        # 按钮行
        btn_row = QHBoxLayout()
        self.btn_test_one = QPushButton("测试选中")
        self.btn_test_one.clicked.connect(self._test_selected)
        btn_row.addWidget(self.btn_test_one)
        self.btn_test_all = QPushButton("测试全部")
        self.btn_test_all.clicked.connect(self._test_all)
        btn_row.addWidget(self.btn_test_all)
        self.btn_set_active = QPushButton("设为当前")
        self.btn_set_active.clicked.connect(self._set_active)
        btn_row.addWidget(self.btn_set_active)
        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.clicked.connect(self._delete_selected)
        btn_row.addWidget(self.btn_delete)
        btn_row.addStretch()
        root.addLayout(btn_row)

        # 添加行
        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("名称:"))
        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("如：我的源")
        add_row.addWidget(self.inp_name)
        add_row.addWidget(QLabel("接口URL:"))
        self.inp_url = QLineEdit()
        self.inp_url.setPlaceholderText("https://域名/api.php/provide/vod/")
        add_row.addWidget(self.inp_url, 1)
        self.btn_add = QPushButton("添加")
        self.btn_add.clicked.connect(self._add_source)
        add_row.addWidget(self.btn_add)
        root.addLayout(add_row)

        self.list_widget.itemChanged.connect(self._on_item_changed)
        self.list_widget.itemDoubleClicked.connect(self._on_double_clicked)
        self._refresh_list()

    # ---- 列表 ----

    def _refresh_list(self):
        self._loading = True
        try:
            self.list_widget.clear()
            active = self.manager.get_active_name()
            sources = self.manager.get_all_sources()
            if not sources:
                item = QListWidgetItem("暂无电影源（点击上方添加）")
                item.setFlags(Qt.NoItemFlags)
                self.list_widget.addItem(item)
                return
            for s in sources:
                item = self._make_row(s, active, test=None)
                self.list_widget.addItem(item)
        finally:
            self._loading = False

    def _make_row(self, s, active, test):
        name = s.get("name") or ""
        url = s.get("url") or ""
        star = "⭐ " if name == active else ""
        en = "✅" if s.get("enabled", True) else "❌"
        suffix = f"  |  {test}" if test else ""
        item = QListWidgetItem(f"{star}{en} {name} - {url}{suffix}")
        item.setData(Qt.UserRole, name)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        item.setCheckState(Qt.Checked if s.get("enabled", True) else Qt.Unchecked)
        return item

    # ---- 勾选/双击 ----

    def _on_item_changed(self, item):
        if self._loading:
            return
        name = item.data(Qt.UserRole)
        if name:
            self.manager.enable_source(name, item.checkState() == Qt.Checked)
            self._notify_changed()

    def _on_double_clicked(self, item):
        """双击行：切换启用（与勾选等价）。"""
        if self._loading:
            return
        name = item.data(Qt.UserRole)
        if not name:
            return
        new_state = item.checkState() != Qt.Checked
        item.setCheckState(Qt.Checked if new_state else Qt.Unchecked)

    # ---- 增删/设当前 ----

    def _add_source(self):
        name = self.inp_name.text().strip()
        url = self.inp_url.text().strip()
        if not name or not url:
            glass_warning(self, "提示", "请输入名称和接口URL")
            return
        if not url.startswith(("http://", "https://")):
            glass_warning(self, "提示", "URL 需以 http:// 或 https:// 开头")
            return
        if not self.manager.add_source(name, url):
            glass_warning(self, "提示", "添加失败：名称重复或无效")
            return
        glass_info(self, "成功", f"已添加源: {name}")
        self.inp_url.clear()
        self.inp_name.clear()
        self._notify_changed()
        self._refresh_list()

    def _delete_selected(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        if not name:
            return
        if not glass_question(self, "确认删除", f"确定删除源 {name} 吗？（永久删除）"):
            return
        self.manager.remove_source(name)
        self._notify_changed()
        self._refresh_list()

    def _set_active(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        if not name:
            return
        self.manager.set_active(name)
        self._notify_changed()
        self._refresh_list()

    # ---- 测源 ----

    def _test_selected(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        src = self._find_source(name)
        if not src:
            return
        self._start_test([(self.list_widget.row(item), src)])

    def _test_all(self):
        jobs = [(i, dict(s)) for i, s in enumerate(self.manager.get_all_sources())]
        if not jobs:
            return
        self._start_test(jobs)

    def _find_source(self, name):
        for s in self.manager.get_all_sources():
            if s.get("name") == name:
                return dict(s)
        return None

    def _start_test(self, jobs):
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        self._test_thread = QThread()
        self._test_worker = _SourceTestWorker(self.manager, jobs)
        self._test_worker.moveToThread(self._test_thread)
        self._test_thread.started.connect(self._test_worker.run)
        self._test_worker.done.connect(self._on_test_done)
        self._test_worker.done.connect(self._test_thread.quit)
        self._test_thread.start()

    def _on_test_done(self, row, name, ok, detail):
        item = self.list_widget.item(row)
        if item is None:
            return
        active = self.manager.get_active_name()
        s = self._find_source(name) or {}
        mark = "✅" if ok else "❌"
        label = f"{mark} {name}" + ("（测源）" if ok else f"（{detail}）")
        # 重建该行（保留勾选状态）
        item.setText(f"{'[当前] ' if name == active else ''}{'✅' if s.get('enabled', True) else '❌'} {name} - {s.get('url', '')}  |  测源: {label}")

    # ---- 变更回调 ----

    def _notify_changed(self):
        if self.on_sources_changed:
            try:
                self.on_sources_changed()
            except Exception as e:
                print(f"[MovieSource] 变更回调失败: {e}")
